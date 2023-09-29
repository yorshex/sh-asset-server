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

from http.server import BaseHTTPRequestHandler, HTTPServer, HTTPStatus
from urllib.parse import urlparse
from urllib.parse import urljoin
from urllib.parse import quote_plus as urlquote
from urllib.parse import parse_qs as parse_url_qs

import os
import os.path as p
import sys
import json # for json.dumps()

from typing import Optional

# for asset reading
import xml.etree.ElementTree as ETree
#from xml.sax.saxutils import escape as xmlescape
import re
import gzip



re_mgSeg = re.compile(r'(\W|^)mgSegment(\W|$)')

def path_is_readable(path):
    return p.exists(path) and p.isfile(path) and os.access(path, os.R_OK)

def dquotes(s):
    return json.dumps(s)


class AdServerAssetReader:
    def __init__(self, asset_dir : str, default_level : Optional[str], do_obstacle_testing : bool):
        self.asset_dir : str = asset_dir
        self.default_level : Optional[str] = default_level
        self.do_obstacle_loading : bool = do_obstacle_testing
        self._templates : Optional[dict[str, dict[str, str]]] = None
        self._templates_mtime : float = 0.0
        self.update_templates()

    def _get_asset_path(self, path):
        return p.join(self.asset_dir, path + '.mp3')

    def _template_exists(self, name):
        return (name is not None) and (name in self._templates.keys())


    def read_asset(self, path : str) -> Optional[bytes]:
        path = self._get_asset_path(path)

        if not path_is_readable(path):
            return

        with open(path, 'rb') as f:
            return f.read()


    def update_templates(self):
        templates_path = self._get_asset_path('templates.xml')

        if not path_is_readable(templates_path):
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

        mgSegment_wrapper = b'''local function __shas_mgSegment_wrapper__(type, depth)
    type = string.gsub(type, "[ !#$%%%%&'()*+,/:;=?@%%[%%]]", function(x)
        return string.format("%%%%%%02X", string.byte(x))
    end)
    return mgSegment("http://%s:8000/segment?type=" .. type .. "%s&filetype=", depth)
end
''' % (bytes(hostname, 'utf-8'), bytes(pv_string, 'utf-8'))

        def repl(matchobj):
            group1 = matchobj.group(1)
            group2 = matchobj.group(2)
            return f'{group1}__shas_mgSegment_wrapper__{group2}'

        return mgSegment_wrapper + bytes(re_mgSeg.sub(repl, room_content.decode('utf-8')), 'utf-8')


    def read_segment(self, segment_type : Optional[str], pv : Optional[int], hostname) -> Optional[bytes]:
        if segment_type is None:
            return

        segment_content = self.read_asset(p.join('segments', segment_type + '.xml'))
        if segment_content is None:
            segment_content = self.read_asset(p.join('segments', segment_type + '.xml.gz'))
            if segment_content is not None:
                segment_content = gzip.decompress(segment_content)

        if segment_content is None:
            return

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
                    if not self._template_exists(template_name):
                        continue
                    del obj.attrib['template']
                    obj.attrib = {**self._templates[template_name], **obj.attrib}

        pv_string = f'&pv={pv}' if pv else ''

        if pv and pv >= 3:
            for obj in segment_root.iter('obstacle'):
                obstacle_type = obj.get('type')
                if obstacle_type is None:
                    continue
                obstacle_path = p.join('obstacles', obstacle_type + '.lua')
                if self.do_obstacle_loading and path_is_readable(self._get_asset_path(obstacle_path)):
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
    def log_request(self, code='-', size='-'):
        if isinstance(code, HTTPStatus):
            code = code.value

        kind = None
        pv_string = ""

        if self.command == "GET":
            urlpath = urljoin('/', self._url.path)
            if urlpath in ("/level", "/room", "/obstacle"):
                kind = urlpath[1:]
            elif urlpath == "/segment" and self._get_query("filetype") == ".xml":
                kind = "segment"
            elif urlpath == "/segment" and self._get_query("filetype") == ".mesh":
                kind = "mesh"

            if kind == "level" and asset_reader.default_level is not None:
                type_string = dquotes(asset_reader.default_level) + ' (default)'
            elif self._get_query("type") is not None:
                type_string = dquotes(self._get_query("type"))
            else:
                type_string = '(unspecified)'

            if self._get_query("pv") is not None:
                pv_string = f', pv.{self._get_query("pv")}'

        if kind is not None:
            self.log_message('get %s %s%s (%s)', kind, type_string, pv_string, str(code))
        else:
            self.log_message('%s (%s)', dquotes(self.requestline), str(code))


    def log_message(self, format, *args):
        message = format % args
        sys.stderr.write("[%s] %s: %s\n" %
                         (self.log_date_time_string(),
                          self.address_string(),
                          message))

    def _send_response(self, response:HTTPResponse):
        self.send_response(response.status)
        response.generate_content_len()
        for key in response.headers.keys():
            self.send_header(key, response.headers[key])
        self.end_headers()
        self.wfile.write(response.content)

    def _conditional_response(self, content : Optional[bytes], content_type : str):
        if content is None:
            return self._send_response(HTTPResponse.not_found())
        return self._send_response(HTTPResponse.ok({'Content-Type': content_type}, content))

    def _get_query(self, name:str) -> Optional[str]:
        if not name in self._queries.keys():
            return None
        return self._queries[name][0]

    def _get_pv(self) -> Optional[int]:
        try:
            return int(self._get_query('pv'))
        except (ValueError, TypeError):
            return None


    def do_GET(self):
        self._url = urlparse(self.path)
        self._hostname = self.headers['Host'].split(':')[0]
        self._queries = parse_url_qs(self._url.query)
        self._pv = self._get_pv()

        match urljoin('/', self._url.path):
            case '/level':
                self._conditional_response(asset_reader.read_level(self._get_query('type'), self._pv, self._hostname), 'text/xml')
            case '/room':
                self._conditional_response(asset_reader.read_room(self._get_query('type'), self._pv, self._hostname), 'text/plain')
            case '/segment':
                if self._get_query('filetype') == '.xml':
                    self._conditional_response(asset_reader.read_segment(self._get_query('type'), self._pv, self._hostname), 'text/xml')
                elif self._get_query('filetype') == '.mesh':
                    self._conditional_response(asset_reader.read_segment_mesh(self._get_query('type')), 'application/octet-stream')
                else:
                    self._send_response(HTTPResponse.not_found())
            case '/obstacle':
                self._conditional_response(asset_reader.read_obstacle(self._get_query('type')), 'text/plain')
            case _:
                self._send_response(HTTPResponse.not_found())



def runAdServer(server_class, handler_class, asset_dir : str, default_level : Optional[str], do_obstacle_loading : bool):
    global asset_reader

    asset_reader = AdServerAssetReader(asset_dir, default_level, do_obstacle_loading)
    server = server_class(("0.0.0.0", 8000), handler_class)

    print("Smash Hit asset server running!")

    try:
        server.serve_forever()
    except Exception as e:
        print("Smash Hit asset server is down:\n" + e)
    except KeyboardInterrupt:
        print("Exiting...")



def main():
    parser = ArgumentParser(prog='python ./levelserver.py', description='Smash Hit Assets Server by yorshex - SH server compatible with Shatter\'s quick test client')
    parser.add_argument('asset_dir', metavar='asset-directory', help='SH asset directory')
    parser.add_argument('-l', '--default-level', dest='default_level', metavar='default-level', help='level that will be accessible at https://localhost:8000/level by default, required for compatibility with Shatter Client')
    parser.add_argument('-o', '--obstacle-loading', dest='do_obstacle_loading', action='store_true', help='Load obstacles from asset directory. Requires Shatter Client version 3.3.0 or later')

    args = parser.parse_args()

    print('WARNING: BE CAREFUL WITH UNTRUSTED XML FILES!\nMORE DETAILS: https://docs.python.org/3/library/xml.html#xml-vulnerabilities\n')

    runAdServer(HTTPServer, AdRequestHandler, p.join(os.getcwd(), args.asset_dir), args.default_level, args.do_obstacle_loading)



if __name__ == '__main__':
    main()
else:
    raise Exception("Asset server isn't a library")
