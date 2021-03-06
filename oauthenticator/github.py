"""
Custom Authenticator to use GitHub OAuth with JupyterHub

Most of the code c/o Kyle Kelley (@rgbkrk)

Extended use of GH attributes by Adam Thornton (athornton@lsst.org)
"""


import json
import os
import re
import string

from tornado.auth import OAuth2Mixin
from tornado import gen, web

from tornado.httputil import url_concat
from tornado.httpclient import HTTPRequest, AsyncHTTPClient

from jupyterhub.auth import LocalAuthenticator

from traitlets import List, Set, Unicode

from .common import next_page_from_links
from .oauth2 import OAuthLoginHandler, OAuthenticator

# Support github.com and github enterprise installations
GITHUB_HOST = os.environ.get('GITHUB_HOST') or 'github.com'
if GITHUB_HOST == 'github.com':
    GITHUB_API = 'api.github.com'
else:
    GITHUB_API = '%s/api/v3' % GITHUB_HOST


def _api_headers(access_token):
    return {"Accept": "application/json",
            "User-Agent": "JupyterHub",
            "Authorization": "token {}".format(access_token)
            }


class GitHubMixin(OAuth2Mixin):
    _OAUTH_AUTHORIZE_URL = "https://%s/login/oauth/authorize" % GITHUB_HOST
    _OAUTH_ACCESS_TOKEN_URL = "https://%s/login/oauth/access_token" % GITHUB_HOST


class GitHubLoginHandler(OAuthLoginHandler, GitHubMixin):
    """The `scope` attribute is inherited from OAuthLoginHandler and is a
    list of scopes requested when we acquire a GitHub token:

    See github_scope.md for details.
    """


class GitHubOAuthenticator(OAuthenticator):

    login_service = "GitHub"

    # deprecated names
    github_client_id = Unicode(config=True, help="DEPRECATED")

    def _github_client_id_changed(self, name, old, new):
        self.log.warn("github_client_id is deprecated, use client_id")
        self.client_id = new
    github_client_secret = Unicode(config=True, help="DEPRECATED")

    def _github_client_secret_changed(self, name, old, new):
        self.log.warn("github_client_secret is deprecated, use client_secret")
        self.client_secret = new

    client_id_env = 'GITHUB_CLIENT_ID'
    client_secret_env = 'GITHUB_CLIENT_SECRET'
    login_handler = GitHubLoginHandler

    github_organization_whitelist = Set(
        config=True,
        help="Automatically whitelist members of selected organizations",
    )
    auth_state_keys = List([
        'email',
        'id',
        'login',
        'name',
    ], config=True,
        help="""keys from a GitHub authorization reply to preserve in
        auth_state.
        
        See GitHub OAuth docs for available keys.
        """
    )

    @gen.coroutine
    def authenticate(self, handler, data=None):
        """We set up auth_state based on additional GitHub info if we
        receive it.
        """
        code = handler.get_argument("code")
        # TODO: Configure the curl_httpclient for tornado
        http_client = AsyncHTTPClient()

        # Exchange the OAuth code for a GitHub Access Token
        #
        # See: https://developer.github.com/v3/oauth/

        # GitHub specifies a POST request yet requires URL parameters
        params = dict(
            client_id=self.client_id,
            client_secret=self.client_secret,
            code=code
        )

        url = url_concat("https://%s/login/oauth/access_token" % GITHUB_HOST,
                         params)

        req = HTTPRequest(url,
                          method="POST",
                          headers={"Accept": "application/json"},
                          body=''  # Body is required for a POST...
                          )

        resp = yield http_client.fetch(req)
        resp_json = json.loads(resp.body.decode('utf8', 'replace'))

        access_token = resp_json['access_token']

        # Determine who the logged in user is
        req = HTTPRequest("https://%s/user" % GITHUB_API,
                          method="GET",
                          headers=_api_headers(access_token)
                          )
        resp = yield http_client.fetch(req)
        resp_json = json.loads(resp.body.decode('utf8', 'replace'))

        username = resp_json["login"]
        # username is now the GitHub userid.
        if not username:
            return None
        # Check if user is a member of any whitelisted organizations.
        # This check is performed here, as it requires `access_token`.
        if self.github_organization_whitelist:
            for org in self.github_organization_whitelist:
                user_in_org = yield self._check_organization_whitelist(org, username, access_token)
                if user_in_org:
                    break
            else:  # User not found in member list for any organisation
                return None
        userdict = {"name": username}
        # Now we set up auth_state
        userdict["auth_state"] = auth_state = {}
        # We may want to do user provisioning in the Lab/Notebook environment.
        #  This next bit is about that.
        #  1) stash the access token
        #  2) use the GitHub ID as the id
        #  3) set up name/email for .gitconfig
        # Store the resulting structure in auth_state
        auth_state['access_token'] = access_token
        #
        # Once you have the access token (and the appropriate scope set in the
        #  handler), you can use that in your subclassed authenticator to
        #  do nifty tricks by making more API calls to get more information
        #  to pass to the spawned Notebook/Lab.

        # The following keys are loaded from the response into auth_state
        # See the GitHub OAuth API for details
        for key in self.auth_state_keys:
            if key in resp_json:
                auth_state[key] = resp_json[key]
        # A public email will return in the initial query (assuming default
        # scope).  Private will not.

        return userdict

    @gen.coroutine
    def _check_organization_whitelist(self, org, username, access_token):
        http_client = AsyncHTTPClient()
        headers = _api_headers(access_token)
        # Get all the members for organization 'org'
        # With empty scope (even if authenticated by an org member), this
        #  will only yield public org members.  You want 'read:org' in order
        #  to be able to iterate through all members.
        next_page = "https://%s/orgs/%s/members" % (GITHUB_API, org)
        while next_page:
            req = HTTPRequest(next_page, method="GET", headers=headers)
            resp = yield http_client.fetch(req)
            resp_json = json.loads(resp.body.decode('utf8', 'replace'))
            next_page = next_page_from_links(resp)
            org_members = set(entry["login"] for entry in resp_json)
            # check if any of the organizations seen so far are in whitelist
            if username in org_members:
                return True
        return False


class LocalGitHubOAuthenticator(LocalAuthenticator, GitHubOAuthenticator):

    """A version that mixes in local system user creation"""
    pass
