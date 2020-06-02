# -* coding: utf-8 -*-

"""
This module provides a native Python client library for Dysart using a
remote-object protocol to access Feature methods.
"""

import binascii
import hashlib
import json
import numbers
import os
import pickle
import random
from typing import *

import requests
import graphviz

# This intentionally-global variable controls how much stuff is printed
verbose = True

class Client:
    """
    """

    # Todo: This is pretty nonstandard for Windows--what's normal there?
    conf_file = os.path.join(os.path.expanduser('~'), '.config',
                                    'dysart', 'client.json')

    cheerful_messages = [
        "Happy measuring!",
        "Have a five-sigma day in lab!",
        "May all your results be statistically significant.",
        "I hope your data looks as good as you do today!",
    ]

    def __init__(self, hostname: str, port: int, verbose: bool = True):
        self.hostname = hostname
        self.port = port
        self.url = f"http://{hostname}:{port}"
        self.verbose = verbose
        try:
            self.load_token()
        except (FileNotFoundError, KeyError):
            self.gen_token()

    def project(self, name: str, token: Optional[str] = None) -> "RemoteProject":
        """Loads a project on the server and generates a remote handle to that
        project.
        Args:
            name: The name of the project on the server
            token: An auth token. If passed `None`, uses default token.

        Returns: A handle to the loaded project.

        """
        url = self.url + '/project'
        data = {
            'project': name,
        }
        response = requests.post(url, json=data, auth=self._auth())
        response.raise_for_status()
        response_data = json.loads(response.content)
        proj = RemoteProject(self, name, token=token)
        for feature_name, methods in response_data['features'].items():
            feature = RemoteFeature(feature_name, proj)
            for method_name in methods:
                method = RemoteProcedureCall(method_name, feature)
                setattr(feature, method_name, method)
            setattr(proj, feature_name, feature)
        if verbose:
            print(f"Loaded project `{name}`.", random.choice(self.cheerful_messages))
            display_graph(response_data['graph'])
        return proj

    def gen_token(self) -> None:
        """Generates a token string.

        Returns: A 32-byte token, as a hex string.

        """
        token = binascii.hexlify(os.urandom(32)).decode()
        self._token = token
        os.makedirs(os.path.dirname(self.conf_file), exist_ok=True)
        try:
            with open(self.conf_file, 'r') as f:
                conf_data = json.loads(f.read())
        except FileNotFoundError:
            conf_data = {}
        conf_data['token'] = self._token
        with open(self.conf_file, 'w') as f:
            json.dump(conf_data, f)
        print("Your token hash is:", self.token_hash(),
              "Please put it in the `tokens` field of your Dysart server's config file!",
              sep='\n')

    def token_hash(self) -> str:
        """Returns a token hash that can be placed on the Dysart server to
        recognize this client. By contrast, self._token is the preimage
        itself.

        Returns: a cryptographic hash of this client's authentication token.

        """
        return hashlib.sha1(self._token.encode('utf-8')).hexdigest()

    def load_token(self) -> None:
        with open(self.conf_file, 'r') as f:
            conf = json.loads(f.read())
            self._token = conf['token']

    def _auth(self) -> Tuple[str, str]:
        """Generates an `auth` argument for requests.

        Returns:

        """
        user = ''
        return (user, self._token)

    def debug(self):
        """Suspends execution of the server process and transfers control
        to a debugger. Invocation of this request should be forbidden by
        the server except with admin authentication

        Returns:

        """
        if verbose:
            print("Attempting to suspend execution... ")
        response = requests.post(self.url + '/debug', auth=self._auth())
        response.raise_for_status()
        if verbose:
            print("Resumed.")


class RemoteProject:
    """A handle to a remote Dysart project
    """

    def __init__(self, client: Client, name: str, token: Optional[str] = None):
        self.client = client
        self.name = name
        self.token = token

    def _repr_svg_(self):
        """Define this to get nice formatting in a Jupyter notebook

        Returns:

        """
        pass


class RemoteFeature:
    """A handle to a remote Dysart feature
    """

    def __init__(self, name: str, project: RemoteProject):
        self.name = name
        self.project = project
        self.url = '/'.join([self.project.client.url, 'feature'])

    def _repr_html_(self):
        data = {
            'project': self.project.name,
            'feature': self.name,
        }
        response = requests.get(self.url, json=data,
                                auth=self.project.client._auth())
        response.raise_for_status()
        return feature_html_table(json.loads(response.content))


class RemoteProcedureCall:

    def __init__(self, name: str, feature: RemoteFeature):
        self.name = name
        self.feature = feature

    def __call__(self, *args, **kwargs):
        proj = self.feature.project
        data = {
            'project': proj.name,
            'feature': self.feature.name,
            'method': self.name,
            'args': args,
            'kwargs': kwargs
        }
        if verbose:
            print("Issuing request... ", end='')
        try:
            response = requests.post(self.feature.url, json=data,
                                     auth=self.feature.project.client._auth())
            value = RemoteProcedureCall.interp_response(response)
            if verbose:
                print('done.')
            return value
        except Exception as e:
            if verbose:
                print('failed.\n', e.args[0])

    @staticmethod
    def interp_response(response):
        response.raise_for_status()
        return pickle.loads(response.content)


def feature_html_table(repr: dict) -> str:
    """

    Args:
        repr: A dictionary representation of a Feature's state with keys
        `name`, `id`, and `results`.

    Returns: An HTML string representing the state of the Feature.

    """
    html = []
    add = html.append

    def table_row(name: str, val: Any):
        if isinstance(val, numbers.Number):
            val_fmt = '{:.5e}'
        else:
            val_fmt = '{}'
        add(f"<tr><td>{name}</td><td>{val_fmt}</td></tr>".format(val))

    add(f"<h2>Feature <code>{repr['name']}</code></h2>")
    add(f"<h3>Id: <code>{repr['id']}</code></h3>")
    if 'results' in repr:
        add(f"<h3>Results</h3>")
        add("<table>")
        for name, val in repr['results'].items():
            table_row(name, val)
        add("</table>")
    if 'diffs' in repr and len(repr['diffs']) >= 1:
        add(f"<h3>Diffs</h3>")
        add("<table>")
        for name, val in repr['diffs'].items():
            table_row(name, val)
        add("</table>")
    return ''.join(html)


def display_graph(edges: List[Tuple[str, str]]):
    d = graphviz.Digraph()
    for edge in edges:
        d.edge(*edge)
    try:
        # This routine is defined if you're running inside a Jupyter notebook.
        # It comes from `IPython.core.display`, but I'm not really sure how
        # it works.
        display(d)
    except NameError:
        # You aren't in a Jupyter notebook, I guess
        pass

if __name__ == '__main__':
    proj = RemoteProject(name='equs_demo',
                         hostname='127.0.0.1',
                         port=8000)
