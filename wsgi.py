#!/usr/bin/env python
#
# GitHub to Bugzilla bridge
#
# Copyright (C) 2015
# Red Hat, Inc.  All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author(s): David Shea <dshea@redhat.com>
# Author(s): Alexander Todorov <atodorov@redhat.com>
#

# For use with mod_wsgi, though it could probably be run ok with the wsgiref
# httpd.

from __future__ import print_function

import os
import re
import json
import hmac
import hashlib
import bugzilla


def application(environ, start_response):
    """ Entry point for mod_wsgi """

    # We always respond with text/plain no matter what, so set that
    response_headers = [('Content-Type', 'text/plain')]

    # Check that all the necessary environment variables are set
    if 'GHBH_BUGZILLA_URL' not in os.environ or \
        'GHBH_BUGZILLA_USERNAME' not in os.environ or \
        'GHBH_BUGZILLA_PASSWORD' not in os.environ:
        print("Missing required environment variables", file=environ['wsgi.errors'])
        start_response('500 Internal Server Error', response_headers)
        return [b'Service not properly configured, please check that all mandatory environment variables are set\n']

    # Check that this request is the right kind of thing: a POST of type
    # application/json with a known length
    if environ['REQUEST_METHOD'] != 'POST':
        start_response('405 Method Not Allowed', response_headers)
        return [b'Only POST messages are accepted\n']

    if 'CONTENT_TYPE' not in environ or environ['CONTENT_TYPE'] != 'application/json':
        print("Invalid content-type %s" % environ.get('CONTENT_TYPE', None),
                file=environ['wsgi.errors'])
        start_response('415 Unsupported Media Type', response_headers)
        return [b'Requests must be of type application/json\n']

    try:
        content_length = int(environ['CONTENT_LENGTH'])
    except (KeyError, ValueError):
        start_response('411 Length required', response_headers)
        return [b'Invalid content length\n']

    # Look for the github headers
    if 'HTTP_X_GITHUB_EVENT' not in environ:
        print("Missing X-Github-Event", file=environ['wsgi.errors'])
        start_response('400 Bad Request', response_headers)
        return [b'Invalid event type\n']

    event_type = environ['HTTP_X_GITHUB_EVENT']

    # Read the post data
    # Errors will be automatically converted to a 500
    post_data = environ['wsgi.input'].read(content_length)

    # If a secret was set, validate the post data
    if 'GHBH_GITHUB_SECRET' in os.environ:
        if 'HTTP_X_HUB_SIGNATURE' not in environ:
            print("Missing signature", file=environ['wsgi.errors'])
            start_response('401 Unauthorized', response_headers)
            return [b'Missing signature\n']

        # Only sha1 is used currently
        if not environ['HTTP_X_HUB_SIGNATURE'].startswith('sha1='):
            print("Signature not sha1", file=environ['wsgi.errors'])
            start_response('401 Unauthorized', response_headers)
            return [b'Invalid signature\n']

        digester = hmac.new(os.environ['GHBH_GITHUB_SECRET'].encode('utf-8'),
                msg=post_data, digestmod=hashlib.sha1)
        if 'sha1=' + digester.hexdigest() != environ['HTTP_X_HUB_SIGNATURE']:
            print("Signature mismatch", file=environ['wsgi.errors'])
            start_response('401 Unauthorized', response_headers)
            return [b'Invalid signature\n']


    home_dir = os.environ.get('OPENSHIFT_DATA_DIR', os.environ.get('HOME', ''))
    cookie_file = os.path.join(home_dir, '.bugzillacookies')
    token_file = os.path.join(home_dir, '.bugzillatoken')
    bz = bugzilla.Bugzilla(
                        url=os.environ['GHBH_BUGZILLA_URL'],
                        cookiefile=cookie_file,
                        tokenfile=token_file
                    )
    try:
        bz.login(os.environ['GHBH_BUGZILLA_USERNAME'], os.environ['GHBH_BUGZILLA_PASSWORD'])
    except bugzilla.BugzillaError as e:
        print("Bugzilla error: %s" % e.message , file=environ['wsgi.errors'])
        start_response('500 Internal Server Error', response_headers)
        return [b'Bugzilla error: %s\n' % e.message]

    # Convert the post data to a string so we can start actually using it
    # JSON is required to be in utf-8, utf-16, or utf-32, but github only ever
    # uses utf-8, praise be, so just go ahead and assume that
    try:
        post_str = post_data.decode('utf-8')
    except UnicodeDecodeError:
        print("Unable to decode JSON", file=environ['wsgi.errors'])
        start_response('400 Bad Request', response_headers)
        return [b'Invalid data\n']

    # Parse the post data
    try:
        event_data = json.loads(post_str)
    except ValueError:
        print("Unable to parse JSON", file=environ['wsgi.errors'])
        start_response('400 Bad Request', response_headers)
        return [b'Invalid data\n']

    # Done with parsing the request, dispatch the data to the event handler
    if event_type == "push":
        post_to_bugzilla(bz, event_data)

    start_response('200 OK', response_headers)
    return [b'']


def get_bugs(data):
    """
        https://developer.github.com/v3/activity/events/types/#pushevent
    """

    bugs = {}

    for commit in data["commits"]:
        sha = commit["id"]
        message = commit["message"]
        summary = message.split("\n")[0].strip()
        body = message.split("\n")[1:]

        # look for a bug in the summary line
        m = re.search(r"\(#\d+(\,.*)*\)", summary)
        if m:
            bugstr = summary[m.start()+2:m.end()-1]

            bug = ''
            for c in bugstr:
                if c.isdigit():
                    bug += c
                else:
                    break

            if bug not in bugs:
                bugs[bug] = [commit]
            else:
                bugs[bug].append(commit)

        # look for bugs in the message body
        for bodyline in body:
            bodyline = bodyline.strip()
            m = re.match(r"^(Resolves|Related|Conflicts):\ +rhbz#\d+.*$", bodyline)

            if not m:
                continue

            actionre = re.search("(Resolves|Related|Conflicts)", bodyline)
            bugre = re.search(r"\d+", bodyline)
            if actionre and bugre:
                action = actionre.group()
                bug = bugre.group()

                if bug not in bugs:
                    bugs[bug] = [commit]
                else:
                    bugs[bug].append(commit)

    return bugs

def get_comments(data):
    """
        https://developer.github.com/v3/activity/events/types/#pushevent
    """
    def indent(lines):
        padding = "    "
        return padding + ('\n'+padding).join(lines.split('\n'))

    bugs = get_bugs(data)
    branch = data["ref"].replace("refs/heads/", "")
    comments = {}

    for bug in bugs.keys():
        for commit in bugs[bug]:
            comment = """
%s
Branch: %s
Author: %s <%s>
Date:   %s

%s
""" % (
        commit["url"], branch,
        commit["author"]["name"], commit["author"]["email"],
        commit["timestamp"].replace("T", " "),
        indent(commit["message"])
    )
            if not bug in comments:
                comments[bug] = comment
            else:
                comments[bug] += comment

    return comments

def post_to_bugzilla(bz, data):
    """
        Return the number of posted bugs for testing purposes
    """
    comments = get_comments(data)
    posts = 0

    for bug_id in comments.keys():
        text = comments[bug_id].strip()
        has_comment = False

        # search by commits for particular branches
        branchre = re.search("Branch: (.+)\n", text)
        if branchre:
            branch = branchre.group()
        else:
            branch = None

        comment_list = bz.get_comments(bug_id)
        comment_list = comment_list['bugs'][bug_id]['comments']

        for bug_comment in comment_list:
            comment_text = bug_comment['text'].strip()
            if branch and comment_text.find(branch) > -1:
                has_comment = True
                break

        if not has_comment:
            bz.update_bugs(bug_id, {'comment': {'comment': text, 'is_private': False}})
            posts += 1

    return posts


# Service, serve thyself
# This is only needed for running outside of mod_wsgi
if __name__ == '__main__':
    from wsgiref.simple_server import make_server

    try:
        httpd = make_server('', os.environ.get('GHBH_HTTP_PORT', 8080), application)
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Exiting on user interrupt")
