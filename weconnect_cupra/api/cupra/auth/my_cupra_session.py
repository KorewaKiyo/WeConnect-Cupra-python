from typing import Dict, Optional, Match

import re
import json
import logging
import requests

from urllib.parse import parse_qsl, urlsplit

from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from oauthlib.common import to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from requests.models import CaseInsensitiveDict
from weconnect_cupra.auth.openid_session import AccessType

from weconnect_cupra.auth.vw_web_session import VWWebSession
from weconnect_cupra.errors import APICompatibilityError, AuthentificationError, RetrievalError, \
    TemporaryAuthentificationError

LOG = logging.getLogger("weconnect_cupra")


class MyCupraSession(VWWebSession):
    def __init__(self, sessionuser, **kwargs):
        super(MyCupraSession, self).__init__(client_id='3c756d46-f1ba-4d78-9f9a-cff0d5292d51@apps_vw-dilab_com',
                                             refresh_url='https://identity.vwgroup.io/oidc/v1/token',
                                             scope='openid profile nickname birthdate phone',
                                             redirect_uri='cupra://oauth-callback',
                                             state=None,
                                             sessionuser=sessionuser,
                                             **kwargs)

        self.headers = CaseInsensitiveDict({
            'accept': '*/*',
            'content-type': 'application/json',
            'user-agent': 'CUPRAApp%20-%20Store/20220503 CFNetwork/1333.0.4 Darwin/21.5.0',
            'accept-language': 'de-de',
            'accept-encoding': 'gzip, deflate, br'
        })

    def login(self):
        authorizationUrl = self.authorizationUrl(url='https://identity.vwgroup.io/oidc/v1/authorize')
        response = self.doWebAuth(authorizationUrl)
        self.fetchTokens('https://identity.vwgroup.io/oidc/v1/token',
                         authorization_response=response
                         )

    def refresh(self):
        self.refreshTokens(
            'https://identity.vwgroup.io/oidc/v1/token',
        )

    def doWebAuth(self, authorizationUrl):  # noqa: C901
        websession: requests.Session = requests.Session()
        retries = Retry(total=self.retries,
                        backoff_factor=2,
                        status_forcelist=[500],
                        raise_on_status=False)
        websession.mount('https://', HTTPAdapter(max_retries=retries))
        websession.headers = CaseInsensitiveDict({
            'user-agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'accept-language': 'en-US,en;q=0.9',
            'accept-encoding': 'gzip, deflate, br'
        })
        while True:
            loginFormResponse: requests.Response = websession.get(authorizationUrl, allow_redirects=False)
            if loginFormResponse.status_code == requests.codes['ok']:
                break
            elif loginFormResponse.status_code == requests.codes['found']:
                if 'Location' in loginFormResponse.headers:
                    authorizationUrl = loginFormResponse.headers['Location']
                else:
                    raise APICompatibilityError('Forwarding without Location in Header')
            elif loginFormResponse.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')
            else:
                raise APICompatibilityError('Retrieving credentials page was not successfull,'
                                            f' status code: {loginFormResponse.status_code}')

        # Find login form on page to obtain inputs
        emailFormRegex = r'<form.+id=\"emailPasswordForm\".*action=\"(?P<formAction>[^\"]+)\"[^>]*>' \
                         r'(?P<formContent>.+?(?=</form>))</form>'
        match: Optional[Match[str]] = re.search(emailFormRegex, loginFormResponse.text, flags=re.DOTALL)
        if match is None:
            raise APICompatibilityError('No login email form found')
        # retrieve target url from form
        target: str = match.groupdict()['formAction']

        # Find all inputs and put those in formData dictionary
        inputRegex = r'<input[\\n\\r\s][^/]*name=\"(?P<name>[^\"]+)\"([\\n\\r\s]value=\"(?P<value>[^\"]+)\")?[^/]*/>'
        formData: Dict[str, str] = {}
        for match in re.finditer(inputRegex, match.groupdict()['formContent']):
            if match.groupdict()['name']:
                formData[match.groupdict()['name']] = match.groupdict()['value']
        if not all(x in ['_csrf', 'relayState', 'hmac', 'email'] for x in formData):
            raise APICompatibilityError('Could not find all required input fields in login page')

        # Set email to the provided username
        formData['email'] = self.sessionuser.username

        # build url from form action
        login2Url: str = 'https://identity.vwgroup.io' + target

        loginHeadersForm: CaseInsensitiveDict = websession.headers.copy()
        loginHeadersForm['Content-Type'] = 'application/x-www-form-urlencoded'

        # Post form content and retrieve credentials page
        login2Response: requests.Response = websession.post(login2Url, headers=loginHeadersForm, data=formData,
                                                            allow_redirects=True)

        if login2Response.status_code != requests.codes['ok']:  # pylint: disable=E1101
            if login2Response.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')
            raise APICompatibilityError('Retrieving credentials page was not successfull,'
                                        f' status code: {login2Response.status_code}')

        credentialsTemplateRegex = r'<script>\s+window\._IDK\s+=\s+\{\s' \
                                   r'(?P<templateModel>.+?(?=\s+\};?\s+</script>))\s+\};?\s+</script>'
        match = re.search(credentialsTemplateRegex, login2Response.text, flags=re.DOTALL)
        if match is None:
            raise APICompatibilityError('No credentials form found')
        if match.groupdict()['templateModel']:
            lineRegex = r'\s*(?P<name>[^\:]+)\:\s+[\'\{]?(?P<value>.+)[\'\}][,]?'
            form2Data: Dict[str, str] = {}
            for match in re.finditer(lineRegex, match.groupdict()['templateModel']):
                if match.groupdict()['name'] == 'templateModel':
                    templateModelString = '{' + match.groupdict()['value'] + '}'
                    if templateModelString.endswith(','):
                        templateModelString = templateModelString[:-len(',')]
                    templateModel = json.loads(templateModelString)
                    if 'relayState' in templateModel:
                        form2Data['relayState'] = templateModel['relayState']
                    if 'hmac' in templateModel:
                        form2Data['hmac'] = templateModel['hmac']
                    if 'emailPasswordForm' in templateModel and 'email' in templateModel['emailPasswordForm']:
                        form2Data['email'] = templateModel['emailPasswordForm']['email']
                    if 'error' in templateModel and templateModel['error'] is not None:
                        if templateModel['error'] == 'validator.email.invalid':
                            raise AuthentificationError('Error during login, email invalid')
                        raise AuthentificationError(f'Error during login: {templateModel["error"]}')
                    if 'registerCredentialsPath' in templateModel and templateModel[
                        'registerCredentialsPath'] == 'register':
                        raise AuthentificationError(
                            f'Error during login, account {self.sessionuser.username} does not exist')
                    if 'errorCode' in templateModel:
                        raise AuthentificationError('Error during login, is the username correct?')
                    if 'postAction' in templateModel:
                        target = templateModel['postAction']
                    else:
                        raise APICompatibilityError('Form does not contain postAction')
                elif match.groupdict()['name'] == 'csrf_token':
                    form2Data['_csrf'] = match.groupdict()['value']
        form2Data['password'] = self.sessionuser.password
        if not all(x in ['_csrf', 'relayState', 'hmac', 'email', 'password'] for x in form2Data):
            raise APICompatibilityError('Could not find all required input fields in login page')

        login3Url = f'https://identity.vwgroup.io/signin-service/v1/{self.client_id}/{target}'

        # Post form content and retrieve userId in forwarding Location
        login3Response: requests.Response = websession.post(login3Url, headers=loginHeadersForm, data=form2Data,
                                                            allow_redirects=False)
        if login3Response.status_code not in (requests.codes['found'], requests.codes['see_other']):
            if login3Response.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')
            raise APICompatibilityError('Forwarding expected (status code 302),'
                                        f' but got status code {login3Response.status_code}')
        if 'Location' not in login3Response.headers:
            raise APICompatibilityError('No url for forwarding in response headers')

        # Parse parametes from forwarding url
        params: Dict[str, str] = dict(parse_qsl(urlsplit(login3Response.headers['Location']).query))

        # Check if error
        if 'error' in params and params['error']:
            errorMessages: Dict[str, str] = {
                'login.errors.password_invalid': 'Password is invalid',
                'login.error.throttled': 'Login throttled, probably too many wrong logins. You have to wait some'
                                         ' minutes until a new login attempt is possible'
            }
            if params['error'] in errorMessages:
                error = errorMessages[params['error']]
            else:
                error = params['error']
            raise AuthentificationError(error)

        # Check for user id
        if 'userId' not in params or not params['userId']:
            if 'updated' in params and params['updated'] == 'dataprivacy':
                raise AuthentificationError('You have to login at myvolkswagen.de and accept the terms and conditions')
            raise APICompatibilityError('No user id provided')
        self.__userId = params['userId']  # pylint: disable=unused-private-member

        # Now follow the forwarding until forwarding URL starts with 'weconnect_cupra://authenticated#'
        afterLoginUrl: str = login3Response.headers['Location']

        consentURL = None
        while True:
            if 'consent' in afterLoginUrl:
                consentURL = afterLoginUrl
            afterLoginResponse = self.get(afterLoginUrl, allow_redirects=False, access_type=AccessType.NONE)
            if afterLoginResponse.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')

            if 'Location' not in afterLoginResponse.headers:
                if consentURL is not None:
                    raise AuthentificationError(
                        'It seems like you need to accept the terms and conditions for the MyCupra service.'
                        f' Try to visit the URL "{consentURL}" or log into the MyCupra smartphone app')
                raise APICompatibilityError('No Location for forwarding in response headers')

            afterLoginUrl = afterLoginResponse.headers['Location']

            if afterLoginUrl.startswith(self.redirect_uri):
                break

        if afterLoginUrl.startswith(self.redirect_uri + '#'):
            queryurl = afterLoginUrl.replace(self.redirect_uri + '#', 'https://egal?')
        else:
            queryurl = afterLoginUrl
        return queryurl

    def fetchTokens(
            self,
            token_url,
            authorization_response=None,
            **kwargs
    ):

        self.parseFromFragment(authorization_response)

        if all(key in self.token for key in ('state', 'id_token', 'access_token', 'code')):
            body: Dict[str, str] = {
                'code': self.token['code'],
                'redirect_uri': self.redirect_uri,
                'client_id': self.client_id,
                'client_secret': 'eb8814e641c81a2640ad62eeccec11c98effc9bccd4269ab7af338b50a94b3a2',
                'grant_type': 'authorization_code',
                'state': self.token['state'],
                'id_token': self.token['id_token']
            }

            loginHeadersForm: CaseInsensitiveDict = self.headers
            loginHeadersForm['content-type'] = 'application/x-www-form-urlencoded; charset=utf-8'

            tokenResponse = self.post(token_url, headers=loginHeadersForm, data=body, allow_redirects=False,
                                      access_type=AccessType.NONE)
            if tokenResponse.status_code != requests.codes['ok']:
                print(tokenResponse.text)
                raise TemporaryAuthentificationError(
                    f'Token could not be fetched due to temporary MyCupra failure: {tokenResponse.status_code}')
            token = self.parseFromBody(tokenResponse.text)

            return token

    def parseFromBody(self, token_response, state=None):
        try:
            token = json.loads(token_response)
        except json.decoder.JSONDecodeError:
            raise TemporaryAuthentificationError(
                'Token could not be refreshed due to temporary MyCupra failure: json could not be decoded')
        if 'accessToken' in token:
            token['access_token'] = token.pop('accessToken')
        if 'idToken' in token:
            token['id_token'] = token.pop('idToken')
        if 'refreshToken' in token:
            token['refresh_token'] = token.pop('refreshToken')
        fixedTokenresponse = to_unicode(json.dumps(token)).encode("utf-8")
        return super(MyCupraSession, self).parseFromBody(token_response=fixedTokenresponse, state=state)

    def refreshTokens(
            self,
            token_url,
            refresh_token=None,
            auth=None,
            timeout=None,
            headers=None,
            verify=True,
            proxies=None,
            **kwargs
    ):
        LOG.info('Refreshing tokens')
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()

        refresh_token = refresh_token or self.refreshToken

        if headers is None:
            headers = self.headers

        body: Dict[str, str] = {
            'client_id': self.client_id,
            'client_secret': 'eb8814e641c81a2640ad62eeccec11c98effc9bccd4269ab7af338b50a94b3a2',
            'grant_type': 'refresh_token',
            'refresh_token': self.token['refresh_token']
        }

        headers['content-type'] = 'application/x-www-form-urlencoded; charset=utf-8'

        tokenResponse = self.post(
            token_url,
            data=body,
            auth=auth,
            timeout=timeout,
            headers=headers,
            verify=verify,
            withhold_token=False,
            proxies=proxies,
            access_type=AccessType.NONE
        )
        if tokenResponse.status_code == requests.codes['unauthorized']:
            raise AuthentificationError('Refreshing tokens failed: Server requests new authorization')
        elif tokenResponse.status_code in (
                requests.codes['internal_server_error'], requests.codes['service_unavailable'],
                requests.codes['gateway_timeout']):
            raise TemporaryAuthentificationError(
                'Token could not be refreshed due to temporary MyCupra failure: {tokenResponse.status_code}')
        elif tokenResponse.status_code == requests.codes['ok']:
            self.parseFromBody(tokenResponse.text)
            if "refresh_token" not in self.token:
                LOG.debug("No new refresh token given. Re-using old.")
                self.token["refresh_token"] = refresh_token
            return self.token
        else:
            raise RetrievalError(f'Status Code from MyCupra while refreshing tokens was: {tokenResponse.status_code}')

    def request(
            self,
            method,
            url,
            data=None,
            headers=None,
            withhold_token=False,
            access_type=AccessType.ACCESS,
            token=None,
            timeout=None,
            **kwargs
    ):
        """Intercept all requests and add userId if present."""
        if not is_secure_transport(url):
            raise InsecureTransportError()
        if self.__userId is not None:
            headers = headers or {}
            headers['user-id'] = self.__userId

        return super(MyCupraSession, self).request(method, url, headers=headers, data=data,
                                                   withhold_token=withhold_token, access_type=access_type, token=token,
                                                   timeout=timeout, **kwargs)

    def getUserinfo(self):
        accessToken = self.token.get('access_token')
        headers = {
            "authorization": f"Bearer {accessToken}",
            "user-agent": "CUPRAApp%20-%20Store/20230404 CFNetwork/1390 Darwin/22.0.0"
        }
        try:
            userinfo_response = requests.get(url="https://identity-userinfo.vwgroup.io/oidc/userinfo", headers=headers)
        except requests.exceptions.ConnectionError as e:
            logging.error(f"Error fetching User Info: {e}")
            return None

        if userinfo_response.status_code == requests.codes['ok']:
            return userinfo_response.json()
        elif userinfo_response.status_code == requests.codes['unauthorized']:
            raise Exception("Token has expired!")

    @property
    def user_id(self):
        if not hasattr(self, "__userId") and self.token is not None:
            userinfo = self.getUserinfo()
            if userinfo is not None:
                self.__userId = userinfo.get("sub")
        elif self.token is None:
            raise Exception("Token does not exist!")
        return self.__userId
