"""Fail-closed build patch for three LiteLLM 1.104.0 API compatibility bugs.

API01: resolve slash-bearing public model IDs using the existing auth/ACL handler.
API07: the final stream usage packet must have choices=[], in sync and async paths.
API23: preserve a known zero cost; unknown costs must remain absent from headers.

Independent of the Responses/SSE and Woow plugin patches. Validate every input,
replacement count, output hash and Python syntax before writing any target.
This is a one-shot build patch: a second application intentionally fails closed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

# relative path: (reviewed input SHA-256, reviewed output SHA-256, edits)
PATCHES = {'proxy/_types.py': ('587d0da479d61ad594fe6bf50fc55aaaf842fcdb025fafcf9fa37028188c4913',
                     'ef49572eafbfbb5819617feb8ee9a42e1c9b20b87ca58e764054a71319583923',
                     (('"/v1/models/{model_id}"', '"/v1/models/{model_id:path}"', 1),
                      ('"/models/{model_id}"', '"/models/{model_id:path}"', 1))),
 'proxy/proxy_server.py': ('32bdd5e335da78426935c72e3c06b1417f26d23709eb180aaddbfc56819cc1e2',
                           'a306974dd69f91c5f0443e1c80d4da084425db660d879606882c0205b7847cad',
                           (('"/v1/models/{model_id}"', '"/v1/models/{model_id:path}"', 1),
                            ('"/models/{model_id}"', '"/models/{model_id:path}"', 1),
                            ('@router.get(\n    "/v1/models/{model_id:path}",',
                             '@router.get("/v1/models/", include_in_schema=False)\n'
                             '@router.get("/models/", include_in_schema=False)\n'
                             'async def model_list_trailing_slash_redirect(request: Request):\n'
                             '    # Preserve the collection redirect instead of treating an empty path as '
                             'a model ID.\n'
                             '    # RedirectResponse preserves the query; the destination retains its '
                             'existing auth.\n'
                             '    return '
                             'RedirectResponse(url=request.url.replace(path=request.url.path[:-1]), '
                             'status_code=307)\n'
                             '\n'
                             '\n'
                             '@router.get(\n'
                             '    "/v1/models/{model_id:path}",',
                             1))),
 'litellm_core_utils/streaming_handler.py': ('a0891d3c589175789eb527fa80a89cdcb5b77dcbab05640ac87234e560819c4f',
                                             '1c31dc3841c051376ea40884f77118a41badea41102ddad076ba9ca9f89ffef6',
                                             (('                response = self.model_response_creator()\n',
                                               '                response = self.model_response_creator()\n'
                                               '                response.choices = []\n',
                                               1),
                                              ('            response: Final = '
                                               'self.model_response_creator()\n',
                                               '            response: Final = '
                                               'self.model_response_creator()\n'
                                               '            response.choices = []\n',
                                               1))),
 'proxy/common_request_processing.py': ('c68c5007d120c5923d9f249aafe988b8e9b4aa206df5a7e683526c4cf766facd',
                                        '745e9f64a9cda4eb5167dd0ea02bc15d53dbee0a55ffb2152882dab7502a5f90',
                                        (('response_cost=hidden_params.get("response_cost") or "",',
                                          'response_cost=hidden_params.get("response_cost"),',
                                          1),
                                         ('                hidden_params.get("response_cost", None) or "",',
                                          '                hidden_params.get("response_cost", None),',
                                          1),
                                         ('recover_response_cost: Final = not response_cost and '
                                          'hidden_params.get("response_cost") is None',
                                          'recover_response_cost: Final = response_cost in (None, "") and '
                                          'hidden_params.get("response_cost") is None',
                                          1),
                                         ('self._response_cost_from_logging_obj(response=response, '
                                          'logging_obj=logging_obj) or ""',
                                          'self._response_cost_from_logging_obj(response=response, '
                                          'logging_obj=logging_obj)',
                                          1))),
 'proxy/auth/auth_utils.py': ('7886adc695a4d37ac9d585421fe8044046f599e9210b8e36543d6323e7f1337b',
                              '7437b32fd3ab66247e389348e88d000acd2170e9c4949ab9316e899069f4dc04',
                              (('    if model is None and not route.lower().startswith("/vertex"):\n'
                                '        google_match = re.search(r"^/models/([^:]+):", route)',
                                '    if (\n'
                                '        model is None\n'
                                '        and not route.lower().startswith("/vertex")\n'
                                '        and not (\n'
                                '            request is not None\n'
                                '            and request.method == "GET"\n'
                                '            and get_request_route_template(request) in '
                                '("/models/{model_id}", "/models/{model_id:path}")\n'
                                '        )\n'
                                '    ):\n'
                                '        # Only the dispatched OpenAI model-info GET is exempt from this '
                                'Google\n'
                                '        # action heuristic. Its existing model_info handler resolves '
                                'aliases and\n'
                                '        # validates model access; key, role and route checks still run '
                                'normally.\n'
                                '        google_match = re.search(r"^/models/([^:]+):", route)',
                                1),))}


def apply(root):
    pending = []
    for name, (expected, result_hash, edits) in PATCHES.items():
        path = Path(root) / name
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise SystemExit('Refusing changed upstream source: ' + name)
        source = raw.decode('utf-8')
        for old, new, count in edits:
            if source.count(old) != count:
                raise SystemExit('Refusing changed patch context: ' + name)
            source = source.replace(old, new)
        result = source.encode('utf-8')
        if hashlib.sha256(result).hexdigest() != result_hash:
            raise SystemExit('Refusing unexpected patched source: ' + name)
        compile(source, str(path), 'exec')
        pending.append((path, result))
    for path, result in pending:
        path.write_bytes(result)
        print(path.name + ': ' + hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
