# -*- coding: utf-8 -*-

from __future__ import absolute_import

import functools
import itertools
import logging

import six
from lxml import etree

from . import py2wsdl, py2xsd, wsa
from .core import SOAPError, SOAPRequest, SOAPResponse
from .utils import uncapitalize

__all__ = ['SOAPDispatcher']

logger = logging.getLogger(__name__)


def call_method(request):
    response = request.method.function(request, request.soap_body)
    if not isinstance(response, SOAPResponse):
        response = SOAPResponse(response)
    return response


class SOAPDispatcher(object):

    def __init__(self, service, middlewares=None, wsdl=None, xsds=None, strict_soap_header=True):
        """
        Args:
            service: the service to expose
            middlewares: the middleware stack
            wsdl: an alternative wsdl to replace the one generated by soapfish
            strict_soap_header: if True an exception will be raised in a header part is not
                in the schema
        """
        self.service = service
        if middlewares is None:
            middlewares = []
        self.middlewares = middlewares
        self.schema_validator = py2xsd.schema_validator(self.service.schema)

        if wsdl is None:
            wsdlelement = py2wsdl.generate_wsdl(self.service)
            self._rewrite_locations(wsdlelement)
            wsdl = etree.tostring(wsdlelement, pretty_print=True)
        self.wsdl = wsdl

        if xsds is None:
            xsds = self._generate_xsds(self.service.schema)
        self.xsds = xsds

        self.strict_soap_header = strict_soap_header

    def middleware(self, i=0):
        if i == len(self.middlewares):
            # at the end call the method
            return call_method
        return functools.partial(self.middlewares[i], next_call=self.middleware(i+1))

    def _parse_soap_content(self, xml):
        SOAP = self.service.version
        try:
            # note : no validation is performed
            envelope = SOAP.Envelope.parsexml(xml)
        except etree.XMLSyntaxError as e:
            raise SOAPError(SOAP.Code.CLIENT, repr(e))
        # Actually this is more a stopgap measure than a real fix. The real
        # fix is to change SOAP.Envelope/ComplexType so it raises some kind of
        # validation error. A missing SOAP body is not allowed by the SOAP
        # specs (according to my interpretation):
        # SOAP 1.1: http://schemas.xmlsoap.org/soap/envelope/
        # SOAP 1.2: http://www.w3.org/2003/05/soap-envelope/
        if envelope.Body is None:
            raise SOAPError(SOAP.Code.CLIENT, "Missing SOAP body")
        return envelope

    def _find_handler_for_request(self, request, body_document):
        SOAP = self.service.version
        soap_action = SOAP.determine_soap_action(request)
        root_tag = None
        if not soap_action:
            root_tag = self._find_root_tag(body_document)
            logger.debug('Soap action not found in http headers, use root tag "%s".', root_tag)
        else:
            logger.debug('Soap action found in http headers: %s', soap_action)
        # TODO: handle invalid xml
        for method in self.service.methods:
            if soap_action:
                if soap_action == method.soapAction:
                    return method
            elif root_tag == method.input:
                return method
        if soap_action is not None:
            error_msg = "Invalid soap action '%s'" % soap_action
        else:
            error_msg = "Missing soap action and invalid root tag '%s'" % root_tag
        raise SOAPError(SOAP.Code.CLIENT, error_msg)

    def _find_root_tag(self, body_document):
        root = body_document
        ns = root.nsmap[root.prefix]
        return root.tag[len('{%s}' % ns):]

    def _parse_header(self, handler, soap_header):
        # TODO return soap fault if header is required but missing in the input
        if soap_header is None:
            return None
        if handler.input_header:
            return soap_header.parse_as(handler.input_header)
        elif self.service.input_header:
            return soap_header.parse_as(self.service.input_header)

    def _parse_input(self, method, message):
        input_parser = method.input
        if isinstance(method.input, six.string_types):
            element = self.service.schema.get_element_by_name(method.input)
            input_parser = element._type
        return input_parser.parse_xmlelement(message)

    def _validate_response(self, return_object, tagname):
        return_object.xml(tagname, namespace=self.service.schema.targetNamespace,
                          elementFormDefault=self.service.schema.elementFormDefault,
                          schema=self.service.schema)  # Validation.

    def _validate_header(self, soap_header):
        if soap_header is None:
            return
        for children in soap_header._xmlelement.getchildren():
            try:
                namespace = children.nsmap[children.prefix]
            except KeyError:
                namespace = None
            if namespace== wsa.NAMESPACE:
                wsa.XML_SCHEMA.assertValid(children)
            else:
                try:
                    self.schema_validator(children)
                except (etree.XMLSyntaxError, etree.DocumentInvalid):
                    if self.strict_soap_header:
                        raise

    def _validate_body(self, soap_body):
        self.schema_validator(soap_body.content())

    def _validate_input(self, envelope):
        self._validate_header(envelope.Header)
        self._validate_body(envelope.Body)

    def dispatch(self, request):
        request_method = request.environ.get('REQUEST_METHOD', '')
        qs = request.environ.get('QUERY_STRING', '')
        if request_method == 'GET' and 'wsdl' in qs:
            return self.handle_wsdl_request(request)
        elif request_method == 'GET' and 'xsd=' in qs:
            return self.handle_xsd_request(request)
        elif request_method == 'POST':
            return self.handle_soap_request(request)
        else:
            return SOAPResponse('bad request', http_status_code=400,
                http_content='bad_request', http_headers={'Content-Type': 'text/plain'})

    def handle_soap_request(self, request):
        request.dispatcher = self
        SOAP = self.service.version

        try:
            soap_envelope = self._parse_soap_content(request.http_content)
            soap_body_content = soap_envelope.Body.content()
            soap_header = soap_envelope.Header

            try:
                self._validate_input(soap_envelope)
            except (etree.XMLSyntaxError, etree.DocumentInvalid) as e:
                raise SOAPError(SOAP.Code.CLIENT, repr(e))

            request.method = self._find_handler_for_request(request, soap_body_content)
            request.soap_header = self._parse_header(request.method, soap_header)
            request.soap_body = self._parse_input(request.method, soap_body_content)
        except SOAPError as ex:
            response = ex
        else:
            response = self.middleware()(request)

        if not isinstance(response, SOAPResponse):
            response = SOAPResponse(response)

        response.http_headers['Content-Type'] = SOAP.CONTENT_TYPE

        if isinstance(response.soap_body, SOAPError):
            error = response.soap_body
            response.http_content = SOAP.get_error_response(error.code, error.message, header=response.soap_header)
            response.http_status_code = 500
        else:
            tagname = uncapitalize(response.soap_body.__class__.__name__)
            #self._validate_response(response.soap_body, tagname)
            # TODO: handle validation results
            if isinstance(request.method.output, six.string_types):
                tagname = request.method.output
            else:
                tagname = uncapitalize(response.content.__class__.__name__)
            response.http_content = SOAP.Envelope.response(tagname, response.soap_body, header=response.soap_header)
        return response

    def handle_wsdl_request(self, request):
        scheme = request.environ.get('X-Forwarded-Proto', request.environ.get('wsgi.url_scheme', 'http'))
        host = request.environ.get('HTTP_HOST')
        wsdl = self.wsdl
        if scheme and host:
            if six.PY3:
                wsdl = wsdl.decode()

            wsdl = wsdl.format(scheme=scheme, host=host)

            if six.PY3:
                wsdl = wsdl.encode()

        return SOAPResponse('wsdl', http_content=wsdl, http_headers={'Content-Type': 'text/xml'})

    def handle_xsd_request(self, request):
        qs = request.environ.get('QUERY_STRING')
        qs = six.moves.urllib.parse.parse_qs(qs)
        xsd = self.xsds[qs.get('xsd')]
        return SOAPResponse('xsd', http_content=xsd, http_headers={'Content-Type': 'text/xml'})

    def _generate_xsds(self, schema, _generated=None):
        if _generated is None:
            _generated = {}

        for _schema in itertools.chain(schema.imports, schema.includes):
            if _schema.location in _generated:
                continue

            xsdelement = py2xsd.generate_xsd(_schema)
            self._rewrite_locations(xsdelement)
            xsd = etree.tostring(xsdelement, pretty_print=True)
            _generated[_schema.location] = xsd
            self._generate_xsds(_schema, _generated)
        return _generated

    def _rewrite_locations(self, element):
        for e in element.xpath('//xsd:import|//xsd:include', namespaces=element.nsmap):
            e.attrib['schemaLocation'] = '?xsd=%s' % e.attrib['schemaLocation']


class WsgiSoapApplication(object):
    HTTP_500 = '500 Internal server error'
    HTTP_200 = '200 OK'
    HTTP_405 = '405 Method Not Allowed'

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    def __call__(self, req_env, start_response, wsgi_url=None):
        content_length = int(req_env.get('CONTENT_LENGTH', '') or 0)
        content = req_env['wsgi.input'].read(content_length)
        soap_request = SOAPRequest(req_env, content)
        response = self.dispatcher.dispatch(soap_request)
        start_response(self._get_http_status(response.http_status_code), response.http_headers.items())
        return [response.http_content]

    def _get_http_status(self, response_status):
        if response_status == 200:
            return self.HTTP_200
        elif response_status == 500:
            return self.HTTP_500
        elif response_status == 405:
            return self.HTTP_405
        else:
            # wsgi wants an http status of len >= 4
            # TODO do a better status code transformation
            return str(response_status) + ' '
