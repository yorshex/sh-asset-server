#!/usr/bin/env python3

'''
Copyright (c) 2023 Maxim "yorshex" Ershov

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.
3. This notice may not be removed or altered from any source distribution.
'''

from argparse import ArgumentParser

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from urllib.parse import urljoin
from urllib.parse import quote_plus as urlquote
from urllib.parse import parse_qs as parse_url_qs

import os
import os.path as p

from typing import Optional

# for asset reading
import xml.etree.ElementTree as ETree
from xml.sax.saxutils import escape as xmlescape
import re
import gzip

_rm_re_pattern = re.compile(r'(\W|^)(mg|conf)Segment\(\s*(?:(["\'])((?:\\.|(?!\3)[^\n\\])*)\3)')

def _path_is_readable(path):
    return p.exists(path) and p.isfile(path) and os.access(path, os.R_OK)

class AdServerAssetReader:
    def __init__(self, asset_dir : str, default_level : Optional[str]):
        self.asset_dir : str = asset_dir
        self.default_level : Optional[str] = default_level
        self._templates : Optional[dict[str, dict[str, str]]] = None
        self._templates_mtime : float = 0.0
        self.update_templates()

    def _get_asset_path(self, path):
        return p.join(self.asset_dir, path + '.mp3')

    def read_asset(self, path : str) -> Optional[bytes]:
        path = self._get_asset_path(path)

        if not _path_is_readable(path):
            return

        with open(path, 'rb') as f:
            return f.read()

    def update_templates(self):
        templates_path = self._get_asset_path('templates.xml')

        if not _path_is_readable(templates_path):
            self._templates = None
            self._templates_mtime = 0.0
            return

        templates_mtime = p.getmtime(templates_path)

        if self._templates_mtime >= templates_mtime:
            return

        templates_content = self.read_asset('templates.xml')

        templates_root : ETree
        try:
            templates_root = ETree.fromstring(templates_content)
        except ETree.ParseError:
            return

        self._templates = {}
        self._templates_mtime = templates_mtime

        for template in templates_root.iter('template'):
            template_name = template.get('name')
            if template_name is None:
                continue
            properties_el = template.find('properties')
            if properties_el is None:
                continue
            properties : dict[str, str] = properties_el.attrib
            self._templates[template_name] = properties

    def read_level(self, level_type : Optional[str], pv : Optional[int], hostname) -> Optional[bytes]:
        if level_type is None:
            if self.default_level is None:
                return
            level_type = self.default_level

        level_content = self.read_asset(p.join('levels', level_type + '.xml'))

        if level_content is None:
            return

        level_root : ETree

        try:
            level_root = ETree.fromstring(level_content)
        except ETree.ParseError:
            return

        pv_string = f'&pv={pv}' if pv else ''

        for rm in level_root.iter('room'):
            rm_type = rm.get('type')
            rm_quoted = urlquote(rm_type)
            rm.attrib['type'] = f'http://{hostname}:8000/room?type={rm_quoted}{pv_string}&ignore='
            #rm.attrib['type'] = xmlescape(f'http://{hostname}:8000/room?type={rm_quoted}&ignore=')

        return ETree.tostring(level_root, encoding='utf-8', method='xml')

    def read_room(self, room_type : Optional[str], pv : Optional[int], hostname) -> Optional[bytes]:
        if room_type is None:
            return

        room_content = self.read_asset(p.join('rooms', room_type + '.lua'))
        if room_content is None:
            return

        pv_string = f'&pv={pv}' if pv else ''

        def _rm_repl(matchobj):
            pref_space_char = matchobj.group(1)
            func_pref = matchobj.group(2)
            quote_char = matchobj.group(3)
            seg_name_quoted = urlquote(matchobj.group(4))
            return f'{pref_space_char}{func_pref}Segment({quote_char}http://{hostname}:8000/segment?type={seg_name_quoted}{pv_string}&filetype={quote_char}'

        return bytes(_rm_re_pattern.sub(_rm_repl, room_content.decode('utf-8')), 'utf-8')

    def read_segment(self, segment_type : Optional[str], pv : Optional[int], hostname) -> Optional[bytes]:
        segment_content = None
        if segment_type is not None:
            segment_content = self.read_asset(p.join('segments', segment_type + '.xml'))
            if segment_content is None:
                segment_content = self.read_asset(p.join('segments', segment_type + '.xml.gz'))
                if segment_content is not None:
                    segment_content = gzip.decompress(segment_content)

        segment_root : ETree
        try:
            segment_root = ETree.fromstring(segment_content)
        except ETree.ParseError:
            return segment_content

        self.update_templates()

        if self._templates is not None:
            for iterator in segment_root.iter('box'), segment_root.iter('obstacle'):
                for obj in iterator:
                    template_name = obj.get('template')
                    if (template_name is not None) and (template_name in self._templates.keys()):
                        del obj.attrib['template']
                        obj.attrib = {**self._templates[template_name], **obj.attrib}

        pv_string = f'&pv={pv}' if pv else ''

        if pv and pv >= 3:
            for obj in segment_root.iter('obstacle'):
                obstacle_type = obj.get('type')
                if obstacle_type is None:
                    continue
                obstacle_path = p.join('obstacles', obstacle_type + '.lua')
                if _path_is_readable(self._get_asset_path(obstacle_path)):
                    type_quoted = urlquote(obstacle_type)
                    obj.attrib['type'] = f'http://{hostname}:8000/obstacle?type={type_quoted}{pv_string}&ignore='
                else:
                    obj.attrib['type'] = f'obstacles/{obstacle_type}'

        return ETree.tostring(segment_root, encoding='utf-8', method='xml')

    def read_segment_mesh(self, segment_type : Optional[str]) -> Optional[bytes]:
        segment_content = self.read_asset(p.join('segments', segment_type + '.mesh'))
        return segment_content

    def read_obstacle(self, obstacle_type) -> Optional[bytes]:
        obstacle_content = self.read_asset(p.join('obstacles', obstacle_type + '.lua'))
        return obstacle_content

asset_reader : AdServerAssetReader

class HTTPResponse:
    def __init__(self, status:int, headers:dict[str, str]={}, content:bytes=b''):
        self.status : int = status
        self.headers : dict[str, str] = headers
        self.content : bytes = content

    def generate_content_len(self):
        self.headers['Content-Length'] = str(len(self.content))

    @classmethod
    def ok(cls, headers:dict[str, str]={}, content:bytes=b''):
        return cls(200, headers, content)

    @classmethod
    def not_found(cls):
        return cls(404,
            {'Content-Type': 'text/html'},
            bytes('<html><head><title>404 Not Found</title></head><body><h1>404 Not Found</h1><p><i>Smash Hit level server</i></p></body></html>', 'utf-8'))

class AdRequestHandler(BaseHTTPRequestHandler):
    def _send_response(self, response:HTTPResponse):
        self.send_response(response.status)
        response.generate_content_len()
        for key in response.headers.keys():
            self.send_header(key, response.headers[key])
        self.end_headers()
        self.wfile.write(response.content)

    def _get_query(self, name:str) -> Optional[str]:
        if not name in self._queries.keys():
            return None
        return self._queries[name][0]

    def _get_pv(self) -> Optional[int]:
        try:
            return int(self._get_query('pv'))
        except (ValueError, TypeError):
            return None

    def _do_level_request(self):
        level_content = asset_reader.read_level(self._get_query('type'), self._get_pv(), self._hostname)
        if level_content is None:
            self._send_response(HTTPResponse.not_found())
            return
        self._send_response(HTTPResponse.ok({'Content-Type': 'text/xml'}, level_content))

    def _do_room_request(self):
        room_content = asset_reader.read_room(self._get_query('type'), self._get_pv(), self._hostname)
        if room_content is None:
            self._send_response(HTTPResponse.not_found())
            return
        self._send_response(HTTPResponse.ok({'Content-Type': 'text/plain'}, room_content))

    def _do_segment_request(self):
        if self._get_query('filetype') == '.xml':
            segment_content = asset_reader.read_segment(self._get_query('type'), self._get_pv(), self._hostname)
            if segment_content is None:
                self._send_response(HTTPResponse.not_found())
                return
            self._send_response(HTTPResponse.ok({'Content-Type': 'text/xml'}, segment_content))
        elif self._get_query('filetype') == '.mesh':
            segment_content = asset_reader.read_segment_mesh(self._get_query('type'))
            if segment_content is None:
                self._send_response(HTTPResponse.not_found())
                return
            self._send_response(HTTPResponse.ok({'Content-Type': 'application/octet-stream'}, segment_content))
        else:
            self._send_response(HTTPResponse.not_found())

    def _do_obstacle_request(self):
        obstacle_content = asset_reader.read_obstacle(self._get_query('type'))
        if obstacle_content is None:
            self._send_response(HTTPResponse.not_found())
            return
        self._send_response(HTTPResponse.ok({'Content-Type': 'text/plain'}, obstacle_content))

    def do_GET(self):
        self._url = urlparse(self.path)
        self._hostname = self.headers['Host'].split(':')[0]
        self._queries = parse_url_qs(self._url.query)

        match urljoin('/', self._url.path):
            case '/level':
                self._do_level_request()
            case '/room':
                self._do_room_request()
            case '/segment':
                self._do_segment_request()
            case '/obstacle':
                self._do_obstacle_request()
            case _:
                self._send_response(HTTPResponse.not_found())

def runAdServer(server_class, handler_class, asset_dir : str, default_level : Optional[str]):
    global asset_reader

    asset_reader = AdServerAssetReader(asset_dir, default_level)
    server = server_class(("0.0.0.0", 8000), handler_class)

    print("Smash Hit level server running!")

    try:
        server.serve_forever()
    except Exception as e:
        print("Smash Hit level server is down:\n" + e)

def main():
    parser = ArgumentParser(prog='python ./levelserver.py', description='Smash Hit Assets Server by yorshex - SH server compatible with Shatter\'s quick test client')
    parser.add_argument('asset_dir', metavar='assets-directory', help='SH asset directory')
    parser.add_argument('-l', '--default-level', dest='default_level', metavar='default-level', help='level that will be accessible at https://localhost:8000/level by default, required for SHBT quick test client compatibility')

    args = parser.parse_args()

    print('WARNING: BE CAREFUL WITH UNTRUSTED XML FILES!\nMORE DETAILS: https://docs.python.org/3/library/xml.html#xml-vulnerabilities\n')

    runAdServer(HTTPServer, AdRequestHandler, p.join(os.getcwd(), args.asset_dir), args.default_level)

if __name__ == '__main__':
    main()
