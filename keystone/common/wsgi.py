# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 OpenStack LLC
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2010 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Utility methods for working with WSGI servers."""

import re
import socket
import sys

import eventlet.wsgi
import routes.middleware
import ssl
import webob.dec
import webob.exc

from keystone.common import config
from keystone.common import logging
from keystone.common import utils
from keystone import exception
from keystone.openstack.common import importutils
from keystone.openstack.common import jsonutils


CONF = config.CONF
LOG = logging.getLogger(__name__)

# Environment variable used to pass the request context
CONTEXT_ENV = 'openstack.context'


# Environment variable used to pass the request params
PARAMS_ENV = 'openstack.params'


class WritableLogger(object):
    """A thin wrapper that responds to `write` and logs."""

    def __init__(self, logger, level=logging.DEBUG):
        self.logger = logger
        self.level = level

    def write(self, msg):
        self.logger.log(self.level, msg)


class Server(object):
    """Server class to manage multiple WSGI sockets and applications."""

    def __init__(self, application, host=None, port=None, threads=1000):
        self.application = application
        self.host = host or '0.0.0.0'
        self.port = port or 0
        self.pool = eventlet.GreenPool(threads)
        self.socket_info = {}
        self.greenthread = None
        self.do_ssl = False
        self.cert_required = False

    def start(self, key=None, backlog=128):
        """Run a WSGI server with the given application."""
        LOG.debug(_('Starting %(arg0)s on %(host)s:%(port)s') %
                  {'arg0': sys.argv[0],
                   'host': self.host,
                   'port': self.port})

        # TODO(dims): eventlet's green dns/socket module does not actually
        # support IPv6 in getaddrinfo(). We need to get around this in the
        # future or monitor upstream for a fix
        info = socket.getaddrinfo(self.host,
                                  self.port,
                                  socket.AF_UNSPEC,
                                  socket.SOCK_STREAM)[0]
        _socket = eventlet.listen(info[-1],
                                  family=info[0],
                                  backlog=backlog)
        if key:
            self.socket_info[key] = _socket.getsockname()
        # SSL is enabled
        if self.do_ssl:
            if self.cert_required:
                cert_reqs = ssl.CERT_REQUIRED
            else:
                cert_reqs = ssl.CERT_NONE
            sslsocket = eventlet.wrap_ssl(_socket, certfile=self.certfile,
                                          keyfile=self.keyfile,
                                          server_side=True,
                                          cert_reqs=cert_reqs,
                                          ca_certs=self.ca_certs)
            _socket = sslsocket

        self.greenthread = self.pool.spawn(self._run,
                                           self.application,
                                           _socket)

    def set_ssl(self, certfile, keyfile=None, ca_certs=None,
                cert_required=True):
        self.certfile = certfile
        self.keyfile = keyfile
        self.ca_certs = ca_certs
        self.cert_required = cert_required
        self.do_ssl = True

    def kill(self):
        if self.greenthread:
            self.greenthread.kill()

    def wait(self):
        """Wait until all servers have completed running."""
        try:
            self.pool.waitall()
        except KeyboardInterrupt:
            pass

    def _run(self, application, socket):
        """Start a WSGI server in a new green thread."""
        log = logging.getLogger('eventlet.wsgi.server')
        try:
            eventlet.wsgi.server(socket, application, custom_pool=self.pool,
                                 log=WritableLogger(log))
        except Exception:
            LOG.exception(_('Server error'))
            raise


class Request(webob.Request):
    pass


class BaseApplication(object):
    """Base WSGI application wrapper. Subclasses need to implement __call__."""

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.

        Any local configuration (that is, values under the [app:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.

        A hypothetical configuration would look like:

            [app:wadl]
            latest_version = 1.3
            paste.app_factory = nova.api.fancy_api:Wadl.factory

        which would result in a call to the `Wadl` class as

            import nova.api.fancy_api
            fancy_api.Wadl(latest_version='1.3')

        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.

        """
        return cls()

    def __call__(self, environ, start_response):
        r"""Subclasses will probably want to implement __call__ like this:

        @webob.dec.wsgify(RequestClass=Request)
        def __call__(self, req):
          # Any of the following objects work as responses:

          # Option 1: simple string
          res = 'message\n'

          # Option 2: a nicely formatted HTTP exception page
          res = exc.HTTPForbidden(detail='Nice try')

          # Option 3: a webob Response object (in case you need to play with
          # headers, or you want to be treated like an iterable, or or or)
          res = Response();
          res.app_iter = open('somefile')

          # Option 4: any wsgi app to be run next
          res = self.application

          # Option 5: you can get a Response object for a wsgi app, too, to
          # play with headers etc
          res = req.get_response(self.application)

          # You can then just return your response...
          return res
          # ... or set req.response and return None.
          req.response = res

        See the end of http://pythonpaste.org/webob/modules/dec.html
        for more info.

        """
        raise NotImplementedError('You must implement __call__')


class Application(BaseApplication):
    @webob.dec.wsgify
    def __call__(self, req):
        arg_dict = req.environ['wsgiorg.routing_args'][1]
        action = arg_dict.pop('action')
        del arg_dict['controller']
        LOG.debug(_('arg_dict: %s'), arg_dict)

        # allow middleware up the stack to provide context & params
        context = req.environ.get(CONTEXT_ENV, {})
        context['query_string'] = dict(req.params.iteritems())
        context['path'] = req.environ['PATH_INFO']
        params = req.environ.get(PARAMS_ENV, {})
        if 'REMOTE_USER' in req.environ:
            context['REMOTE_USER'] = req.environ['REMOTE_USER']
        elif context.get('REMOTE_USER', None) is not None:
            del context['REMOTE_USER']
        params.update(arg_dict)

        # TODO(termie): do some basic normalization on methods
        method = getattr(self, action)

        # NOTE(vish): make sure we have no unicode keys for py2.6.
        params = self._normalize_dict(params)

        try:
            result = method(context, **params)
        except exception.Unauthorized as e:
            LOG.warning(_("Authorization failed. %s from %s")
                        % (e, req.environ['REMOTE_ADDR']))
            return render_exception(e)
        except exception.Error as e:
            LOG.warning(e)
            return render_exception(e)
        except TypeError as e:
            logging.exception(e)
            return render_exception(exception.ValidationError(e))
        except Exception as e:
            logging.exception(e)
            return render_exception(exception.UnexpectedError(exception=e))

        if result is None:
            return render_response(status=(204, 'No Content'))
        elif isinstance(result, basestring):
            return result
        elif isinstance(result, webob.Response):
            return result
        elif isinstance(result, webob.exc.WSGIHTTPException):
            return result

        response_code = self._get_response_code(req)
        return render_response(body=result, status=response_code)

    def _get_response_code(self, req):
        req_method = req.environ['REQUEST_METHOD']
        controller = importutils.import_class('keystone.common.controller')
        code = None
        if isinstance(self, controller.V3Controller) and req_method == 'POST':
            code = (201, 'Created')
        return code

    def _normalize_arg(self, arg):
        return str(arg).replace(':', '_').replace('-', '_')

    def _normalize_dict(self, d):
        return dict([(self._normalize_arg(k), v)
                     for (k, v) in d.iteritems()])

    def assert_admin(self, context):
        if not context['is_admin']:
            try:
                user_token_ref = self.token_api.get_token(
                    context=context, token_id=context['token_id'])
            except exception.TokenNotFound as e:
                raise exception.Unauthorized(e)

            creds = user_token_ref['metadata'].copy()

            try:
                creds['user_id'] = user_token_ref['user'].get('id')
            except AttributeError:
                logging.debug('Invalid user')
                raise exception.Unauthorized()

            try:
                creds['tenant_id'] = user_token_ref['tenant'].get('id')
            except AttributeError:
                logging.debug('Invalid tenant')
                raise exception.Unauthorized()

            # NOTE(vish): this is pretty inefficient
            creds['roles'] = [self.identity_api.get_role(context, role)['name']
                              for role in creds.get('roles', [])]
            # Accept either is_admin or the admin role
            self.policy_api.enforce(context, creds, 'admin_required', {})


class Middleware(Application):
    """Base WSGI middleware.

    These classes require an application to be
    initialized that will be called next.  By default the middleware will
    simply call its wrapped app, or you can override __call__ to customize its
    behavior.

    """

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.

        Any local configuration (that is, values under the [filter:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.

        A hypothetical configuration would look like:

            [filter:analytics]
            redis_host = 127.0.0.1
            paste.filter_factory = nova.api.analytics:Analytics.factory

        which would result in a call to the `Analytics` class as

            import nova.api.analytics
            analytics.Analytics(app_from_paste, redis_host='127.0.0.1')

        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.

        """
        def _factory(app):
            conf = global_config.copy()
            conf.update(local_config)
            return cls(app)
        return _factory

    def __init__(self, application):
        self.application = application

    def process_request(self, request):
        """Called on each request.

        If this returns None, the next application down the stack will be
        executed. If it returns a response then that response will be returned
        and execution will stop here.

        """
        return None

    def process_response(self, request, response):
        """Do whatever you'd like to the response, based on the request."""
        return response

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, request):
        try:
            response = self.process_request(request)
            if response:
                return response
            response = request.get_response(self.application)
            return self.process_response(request, response)
        except exception.Error as e:
            LOG.warning(e)
            return render_exception(e)
        except TypeError as e:
            LOG.exception(e)
            return render_exception(exception.ValidationError(e))
        except Exception as e:
            LOG.exception(e)
            return render_exception(exception.UnexpectedError(exception=e))


class Debug(Middleware):
    """Helper class for debugging a WSGI application.

    Can be inserted into any WSGI application chain to get information
    about the request and response.

    """

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, req):
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug('%s %s %s', ('*' * 20), 'REQUEST ENVIRON', ('*' * 20))
            for key, value in req.environ.items():
                LOG.debug('%s = %s', key, mask_password(value,
                                                        is_unicode=True))
            LOG.debug('')
            LOG.debug('%s %s %s', ('*' * 20), 'REQUEST BODY', ('*' * 20))
            for line in req.body_file:
                LOG.debug(mask_password(line))
            LOG.debug('')

        resp = req.get_response(self.application)
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug('%s %s %s', ('*' * 20), 'RESPONSE HEADERS', ('*' * 20))
            for (key, value) in resp.headers.iteritems():
                LOG.debug('%s = %s', key, value)
            LOG.debug('')

        resp.app_iter = self.print_generator(resp.app_iter)

        return resp

    @staticmethod
    def print_generator(app_iter):
        """Iterator that prints the contents of a wrapper string."""
        LOG.debug('%s %s %s', ('*' * 20), 'RESPONSE BODY', ('*' * 20))
        for part in app_iter:
            LOG.debug(part)
            yield part


class Router(object):
    """WSGI middleware that maps incoming requests to WSGI apps."""

    def __init__(self, mapper):
        """Create a router for the given routes.Mapper.

        Each route in `mapper` must specify a 'controller', which is a
        WSGI app to call.  You'll probably want to specify an 'action' as
        well and have your controller be an object that can route
        the request to the action-specific method.

        Examples:
          mapper = routes.Mapper()
          sc = ServerController()

          # Explicit mapping of one route to a controller+action
          mapper.connect(None, '/svrlist', controller=sc, action='list')

          # Actions are all implicitly defined
          mapper.resource('server', 'servers', controller=sc)

          # Pointing to an arbitrary WSGI app.  You can specify the
          # {path_info:.*} parameter so the target app can be handed just that
          # section of the URL.
          mapper.connect(None, '/v1.0/{path_info:.*}', controller=BlogApp())

        """
        # if we're only running in debug, bump routes' internal logging up a
        # notch, as it's very spammy
        if CONF.debug:
            logging.getLogger('routes.middleware').setLevel(logging.INFO)

        self.map = mapper
        self._router = routes.middleware.RoutesMiddleware(self._dispatch,
                                                          self.map)

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, req):
        """Route the incoming request to a controller based on self.map.

        If no match, return a 404.

        """
        return self._router

    @staticmethod
    @webob.dec.wsgify(RequestClass=Request)
    def _dispatch(req):
        """Dispatch the request to the appropriate controller.

        Called by self._router after matching the incoming request to a route
        and putting the information into req.environ.  Either returns 404
        or the routed WSGI app's response.

        """
        match = req.environ['wsgiorg.routing_args'][1]
        if not match:
            return render_exception(
                exception.NotFound(_('The resource could not be found.')))
        app = match['controller']
        return app


class ComposingRouter(Router):
    def __init__(self, mapper=None, routers=None):
        if mapper is None:
            mapper = routes.Mapper()
        if routers is None:
            routers = []
        for router in routers:
            router.add_routes(mapper)
        super(ComposingRouter, self).__init__(mapper)


class ComposableRouter(Router):
    """Router that supports use by ComposingRouter."""

    def __init__(self, mapper=None):
        if mapper is None:
            mapper = routes.Mapper()
        self.add_routes(mapper)
        super(ComposableRouter, self).__init__(mapper)

    def add_routes(self, mapper):
        """Add routes to given mapper."""
        pass


class ExtensionRouter(Router):
    """A router that allows extensions to supplement or overwrite routes.

    Expects to be subclassed.
    """
    def __init__(self, application, mapper=None):
        if mapper is None:
            mapper = routes.Mapper()
        self.application = application
        self.add_routes(mapper)
        mapper.connect('{path_info:.*}', controller=self.application)
        super(ExtensionRouter, self).__init__(mapper)

    def add_routes(self, mapper):
        pass

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.

        Any local configuration (that is, values under the [filter:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.

        A hypothetical configuration would look like:

            [filter:analytics]
            redis_host = 127.0.0.1
            paste.filter_factory = nova.api.analytics:Analytics.factory

        which would result in a call to the `Analytics` class as

            import nova.api.analytics
            analytics.Analytics(app_from_paste, redis_host='127.0.0.1')

        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.

        """
        def _factory(app):
            conf = global_config.copy()
            conf.update(local_config)
            return cls(app)
        return _factory


def render_response(body=None, status=None, headers=None):
    """Forms a WSGI response."""
    headers = headers or []
    headers.append(('Vary', 'X-Auth-Token'))

    if body is None:
        body = ''
        status = status or (204, 'No Content')
    else:
        body = jsonutils.dumps(body, cls=utils.SmarterEncoder)
        headers.append(('Content-Type', 'application/json'))
        status = status or (200, 'OK')

    return webob.Response(body=body,
                          status='%s %s' % status,
                          headerlist=headers)


def render_exception(error):
    """Forms a WSGI response based on the current error."""
    body = {'error': {
        'code': error.code,
        'title': error.title,
        'message': str(error)
    }}
    if isinstance(error, exception.AuthPluginException):
        body['error']['identity'] = error.authentication
    return render_response(status=(error.code, error.title), body=body)


_RE_PASS = re.compile(r'([\'"].*?password[\'"]\s*:\s*u?[\'"]).*?([\'"])',
                      re.DOTALL)


def mask_password(message, is_unicode=False, secret="***"):
    """Replace password with 'secret' in message.

    :param message: The string which include security information.
    :param is_unicode: Is unicode string ?
    :param secret: substitution string default to "***".
    :returns: The string

    For example:
       >>> mask_password('"password" : "aaaaa"')
       '"password" : "***"'
       >>> mask_password("'original_password' : 'aaaaa'")
       "'original_password' : '***'"
       >>> mask_password("u'original_password' :   u'aaaaa'")
       "u'original_password' :   u'***'"
    """
    if is_unicode:
        message = unicode(message)
    # Match the group 1,2 and replace all others with 'secret'
    secret = r"\g<1>" + secret + r"\g<2>"
    result = _RE_PASS.sub(secret, message)
    return result
