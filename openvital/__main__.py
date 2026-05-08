import sys
import json
import importlib
import os
import traceback
import copy
import gzip
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

filter_folder = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'filters')
server_port = 3000

for arg in sys.argv[1:]:
    if os.path.isdir(arg):
        filter_folder = arg
    elif arg.isdecimal():
        if 0 < int(arg) < 65535:
            server_port = int(arg)

print('filter folder : ' + str(filter_folder))
print('server port : ' + str(server_port))

sys.path.insert(0, filter_folder)

cfgs = {}  # Current settings and data for the module (the corresponding invokeid)
default_cfgs = {}  # Default settings and data for the module
mods = {}  # Loaded modules
mod_cfgs = []  # load module cfgs

# Filter modules whose extras (deps not in the base install) gate them.
# Keep this in sync with [project.optional-dependencies] in pyproject.toml
# so the install hint we print on ImportError is correct.
_FILTER_EXTRAS = {
    'abp_hpi':                  'ml-torch',     # torch (HpiModel)
    'ecg_beat_noise_detector':  'ml-torch',     # torch (UniMSNet.pth)
    'sv_dlapco':                'ml-torch',     # torch
    'ecg_classifier':           'ml-tf',        # tensorflow + keras
    'abp_ppv':                  'signal',       # scipy.signal/interpolate
    'pleth_spi':                'signal',       # scipy.stats
}

# load filters
for root, dirs, files in os.walk(filter_folder):
    for filename in files:
        #filepath = os.path.join(root, filename)
        if filename[-3:] != ".py":
            continue

        m_modname = filename[:-3]  #filepath[:-3].replace(os.sep, ".")
        print('importing ' + m_modname)
        try:
            o = importlib.import_module(m_modname)
        except ImportError as e:
            # A filter's extra-dependency is not installed. Skip this filter
            # (so the server still starts) and tell the user how to enable it.
            extra = _FILTER_EXTRAS.get(m_modname)
            if extra:
                print(f'  [skipped] {m_modname}: missing optional dep — '
                      f'install with `pip install openvital[{extra}]` '
                      f'(underlying error: {e})')
            else:
                print(f'  [skipped] {m_modname}: import failed — {e}')
            continue
        mods[m_modname] = o  # modules are saved for later reloading

        if not hasattr(o, 'cfg'):
            continue
        if not hasattr(o, 'run'):
            continue

        if m_modname not in default_cfgs:  # if the module was first loaded or changed?
            default_cfgs[m_modname] = copy.deepcopy(o.cfg)
        cfg = copy.deepcopy(default_cfgs[m_modname])

        if 'name' in cfg:
            name = cfg['name']
        else:
            name = m_modname
        if 'group' in cfg:
            group = cfg['group']
        else:
            group = ''
        if 'desc' in cfg:
            desc = cfg['desc']
        else:
            desc = ''
        if 'interval' in cfg:
            interval = cfg['interval']
        else:
            interval = 60
        if 'overlap' in cfg:
            overlap = cfg['overlap']
        else:
            overlap = 0
        if 'inputs' in cfg:
            inputs = cfg['inputs']
        else:
            inputs = []
        if 'options' in cfg:
            opt = cfg['options']
        else:
            opt = []
        if 'license' in cfg:
            licen = cfg['license']
        else:
            licen = ""
        if 'reference' in cfg:
            refer = cfg['reference']
        else:
            refer = ""
        if 'outputs' in cfg:
            outputs = cfg['outputs']
        else:
            outputs = []

        mod_cfgs.append({
            "modname": m_modname,
            "name": name,
            "group": group,
            "desc": desc,
            "interval": interval,
            "overlap": overlap,
            "inputs": inputs,
            "options": opt,
            "outputs": outputs,
            "license": licen,
            "reference": refer
        })

# stdlib http.server is a single-threaded, zero-deps replacement for the old
# sanic-based server. The wire protocol (gzip JSON in / out) is unchanged so
# existing clients (Vital Recorder etc.) need no changes. Single-thread is
# intentional — it preserves the prior sanic-event-loop semantics where state
# (cfgs, default_cfgs) is mutated without locks.
class FilterHandler(BaseHTTPRequestHandler):
    # silence default access log (was access_log=False in sanic)
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b'', ctype='application/octet-stream'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode('utf-8')
        self._send(code, body, 'application/json')

    def do_GET(self):
        if self.path == '/':
            self._send_json(200, mod_cfgs)
        else:
            self._send(404)

    def do_POST(self):
        modname = unquote(self.path.lstrip('/'))
        m_modname = os.path.basename(modname)

        if m_modname not in mods:
            # The filter was skipped at startup (missing optional dep). Tell
            # the caller which extra they need rather than 500-erroring.
            extra = _FILTER_EXTRAS.get(m_modname)
            msg = (f'filter {m_modname!r} is unavailable: missing optional '
                   f'dependency. Install with `pip install '
                   f'openvital[{extra}]`.' if extra else
                   f'filter {m_modname!r} is not loaded.')
            self._send_json(503, {'error': msg})
            return

        try:
            clen = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(clen) if clen else b''
            posts = json.loads(gzip.decompress(raw).decode('utf-8'))
        except Exception as e:
            print(f'bad request body: {e}')
            self._send(400)
            return

        try:
            invokeid = posts['invokeid']
            inp = posts['inputs']
            o = mods[m_modname]

            if invokeid not in cfgs:
                if m_modname not in default_cfgs:
                    default_cfgs[m_modname] = copy.deepcopy(o.cfg)
                cfgs[invokeid] = copy.deepcopy(default_cfgs[m_modname])
            cfg = cfgs[invokeid]

            cfg['interval'] = posts['interval']
            cfg['overlap'] = posts['overlap']
            cfg['invokeid'] = invokeid

            opt = posts.get('options', [])
            ret = o.run(inp, opt, cfg)
            body = gzip.compress(json.dumps(ret).encode('utf-8'))
            self._send(200, body)
        except Exception as e:
            print(f'filter {m_modname!r} run error: {e}')
            traceback.print_exc()
            self._send(500)


if __name__ == "__main__":
    server = HTTPServer(('0.0.0.0', server_port), FilterHandler)
    print(f'serving on http://0.0.0.0:{server_port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
