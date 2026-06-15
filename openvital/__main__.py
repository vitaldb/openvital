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

from openvital.fhir_adapter import (
    params_to_filter_input,
    result_to_bundle,
    operation_definition,
    capability_statement,
    operation_outcome,
    modname_to_opname,
    opname_to_modname,
)

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

default_cfgs = {}  # Default settings and data for each module
mods = {}  # Loaded modules
mod_cfgs = []  # Filter list metadata (returned to clients on GET /)

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

# stdlib http.server is a zero-deps replacement for the old sanic-based
# server. The wire protocol (gzip JSON in / out) is unchanged so existing
# clients (Vital Recorder etc.) need no changes. The handler is stateless —
# every POST starts from a fresh deepcopy of default_cfgs[modname], so the
# server is safe under concurrency (Lambda containers, multiple workers)
# and free of the previous unbounded `cfgs[invokeid]` leak. Filters that
# need cross-chunk continuity (only pleth_pvi mutates cfg in-place) must
# rely on a window long enough to converge within a single call.
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
            return
        if self.path == '/fhir/metadata':
            self._send_fhir(200, capability_statement(mod_cfgs))
            return
        if self.path.startswith('/fhir/OperationDefinition/'):
            opname = unquote(self.path[len('/fhir/OperationDefinition/'):])
            modname = opname_to_modname(opname)
            cfg_entry = next((c for c in mod_cfgs if c['modname'] == modname), None)
            if cfg_entry is None or modname not in mods:
                self._send_fhir(404, operation_outcome(
                    'error', 'not-found',
                    f"OperationDefinition/{opname} not found"))
                return
            self._send_fhir(200, operation_definition(modname, cfg_entry))
            return
        self._send(404)

    def do_POST(self):
        if self.path.startswith('/fhir/$') or self.path.startswith('/fhir/%24'):
            self._handle_fhir_invoke()
            return

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

            if m_modname not in default_cfgs:
                default_cfgs[m_modname] = copy.deepcopy(o.cfg)
            cfg = copy.deepcopy(default_cfgs[m_modname])

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

    # ---- FHIR R4 Operation handler -----------------------------------------
    # Path:   POST /fhir/$<operation-name>     (Parameters in, Bundle out)
    # Filter modname is reconstructed from operation-name by replacing '-' →
    # '_'. Body may be raw or gzip-compressed (Content-Encoding header).

    def _handle_fhir_invoke(self):
        # Strip leading '/fhir/$' (also handle URL-encoded form '/fhir/%24')
        path = unquote(self.path)
        opname = path[len('/fhir/$'):]
        if '?' in opname:
            opname = opname.split('?', 1)[0]
        modname = opname_to_modname(opname)

        if modname not in mods:
            extra = _FILTER_EXTRAS.get(modname)
            if extra:
                msg = (f"filter {modname!r} is unavailable: missing optional "
                       f"dependency. Install with `pip install "
                       f"openvital[{extra}]`.")
            else:
                msg = f"filter {modname!r} is not loaded."
            self._send_fhir(503, operation_outcome('error', 'not-supported', msg))
            return

        try:
            clen = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(clen) if clen else b''
            if (self.headers.get('Content-Encoding') or '').lower() == 'gzip':
                raw = gzip.decompress(raw)
            params = json.loads(raw.decode('utf-8'))
        except Exception as e:
            self._send_fhir(400, operation_outcome(
                'error', 'invalid', f'bad request body: {e}'))
            return

        cfg_entry = next((c for c in mod_cfgs if c['modname'] == modname), None)
        if cfg_entry is None:
            self._send_fhir(500, operation_outcome(
                'error', 'exception', f'cfg metadata missing for {modname!r}'))
            return
        input_names = [i.get('name', f'in{j}')
                       for j, i in enumerate(cfg_entry.get('inputs', []))]
        output_metas = list(cfg_entry.get('outputs', []))

        try:
            o = mods[modname]
            if modname not in default_cfgs:
                default_cfgs[modname] = copy.deepcopy(o.cfg)
            defaults = default_cfgs[modname]

            inp, opt, cfg, ctx = params_to_filter_input(
                params, defaults, input_names)
            result = o.run(inp, opt, cfg)
            bundle = result_to_bundle(result, output_metas, ctx)
            self._send_fhir(200, bundle,
                            accept_encoding=self.headers.get('Accept-Encoding', ''))
        except ValueError as e:
            self._send_fhir(400, operation_outcome('error', 'invalid', str(e)))
        except Exception as e:
            print(f"filter {modname!r} fhir invoke error: {e}")
            traceback.print_exc()
            self._send_fhir(500, operation_outcome('error', 'exception', str(e)))

    def _send_fhir(self, code, obj, accept_encoding=''):
        body = json.dumps(obj).encode('utf-8')
        gz = 'gzip' in (accept_encoding or '').lower()
        if gz:
            body = gzip.compress(body)
        self.send_response(code)
        self.send_header('Content-Type', 'application/fhir+json')
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(('0.0.0.0', server_port), FilterHandler)
    print(f'serving on http://0.0.0.0:{server_port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
