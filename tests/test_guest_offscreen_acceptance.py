"""CPU only: never load EGL/GLES, even if a production regression attempts it."""
import argparse
import ast
import ctypes as c
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'diagnostic' / 'guest_offscreen_acceptance.py'
# Guard import as well as all test bodies. A real graphics-library load is a failure.
NO_NATIVE = patch.object(c, 'CDLL', side_effect=RuntimeError('native library loading forbidden in CPU tests'))
NO_NATIVE.start()
spec = importlib.util.spec_from_file_location('acceptance', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def put(pointer, value):
    pointer._obj.value = value


class FakeAPI:
    """Raw EGL/GL ABI stub, not an alternate acceptance algorithm.

    Pixels are hard-coded independently for the two palettes. The production
    Harness owns setup, shader logs, bounds, expectations, errors and teardown.
    Unknown functions fail closed instead of silently returning success.
    """
    VOID = set('''glShaderSource glCompileShader glAttachShader glBindAttribLocation
        glLinkProgram glUseProgram glBindBuffer glBufferData glVertexAttribPointer
        glEnableVertexAttribArray glDisableVertexAttribArray glViewport glDisable
        glClear glDrawArrays glFinish glPixelStorei glDeleteBuffers glDeleteProgram
        glDeleteShader glBindFramebuffer'''.split())
    BOOL = set('''eglInitialize eglBindAPI eglChooseConfig eglMakeCurrent
        eglQuerySurface eglDestroyContext eglDestroySurface eglTerminate
        eglReleaseThread'''.split())
    OTHER = set('''platform_display eglCreatePbufferSurface eglCreateContext
        eglQueryString glGetString glCheckFramebufferStatus glCreateShader
        glCreateProgram glGetUniformLocation glGenBuffers glGetShaderiv
        glGetProgramiv glGetShaderInfoLog glGetProgramInfoLog glClearColor
        glUniform4f glReadPixels'''.split())

    def __init__(self, *, fail=None, gl_error=None, egl_error=None,
                 dimensions=(64, 64), fbo=0x8CD5, stale=False, corrupt=False,
                 renderer=b'zink (gfxstream Mali reported-name)', shader_ok=True,
                 link_ok=True, null=None):
        self.calls = []
        self.fail, self.gl_error, self.egl_error = fail, gl_error, egl_error
        self.dimensions, self.fbo = dimensions, fbo
        self.stale, self.corrupt, self.renderer = stale, corrupt, renderer
        self.shader_ok, self.link_ok, self.null = shader_ok, link_ok, null
        self.pending_gl, self.pending_egl = 0, 0x3000
        self.next_shader = 10
        self.phase = 0
        self.background = self.triangle = None
        self.reads = []

    def eglGetError(self):
        error, self.pending_egl = self.pending_egl, 0x3000
        return error

    def glGetError(self):
        error, self.pending_gl = self.pending_gl, 0
        return error

    def __getattr__(self, name):
        if name not in self.VOID | self.BOOL | self.OTHER:
            raise AttributeError(name)

        def call(*args):
            self.calls.append(name)
            release = name == 'eglMakeCurrent' and args[-1] is None
            operation = 'release-current' if release else name
            if operation == self.gl_error:
                self.pending_gl = 0x0502
            if operation == self.egl_error:
                self.pending_egl = 0x3002
            if operation == self.fail:
                self.pending_egl = 0x3002
                return 0
            if operation == self.null:
                return 0
            if name == 'platform_display':
                return 1
            if name == 'eglInitialize':
                put(args[1], 1)
                put(args[2], 5)
            elif name == 'eglChooseConfig':
                put(args[2], 2)
                put(args[4], 1)
            elif name == 'eglCreatePbufferSurface':
                return 3
            elif name == 'eglCreateContext':
                return 4
            elif name == 'eglQuerySurface':
                put(args[-1], self.dimensions[0 if args[2] == 0x3057 else 1])
            elif name == 'eglQueryString':
                return b'complete fake EGL identity string'
            elif name == 'glGetString':
                return self.renderer if args[0] == 0x1F01 else b'complete fake GL identity string'
            elif name == 'glCheckFramebufferStatus':
                return self.fbo
            elif name == 'glCreateShader':
                self.next_shader += 1
                return self.next_shader
            elif name == 'glCreateProgram':
                return 20
            elif name == 'glGetUniformLocation':
                return 0
            elif name == 'glGenBuffers':
                put(args[1], 30)
            elif name in ('glGetShaderiv', 'glGetProgramiv'):
                value = 25 if args[1] == 0x8B84 else int(
                    self.link_ok if name == 'glGetProgramiv' else self.shader_ok)
                put(args[2], value)
            elif name in ('glGetShaderInfoLog', 'glGetProgramInfoLog'):
                args[3].value = b'CPU compiler/linker log'
                put(args[2], len(args[3].value))
            elif name == 'glUniform4f':
                self.triangle = tuple(args[1:])
                self.phase = 0 if self.triangle == (1, 0, 0, 1) else 1
            elif name == 'glClearColor':
                self.background = tuple(args)
            elif name == 'glReadPixels':
                x, y = args[:2]
                self.reads.append((x, y, self.triangle, self.background))
                phase = 0 if self.stale else self.phase
                # Not derived from production palette()/expected arguments.
                if (x, y) == (32, 32):
                    value = (255, 0, 0, 255) if phase == 0 else (0, 255, 0, 255)
                elif (x, y) == (0, 63):
                    value = (0, 0, 255, 255) if phase == 0 else (255, 0, 255, 255)
                else:
                    raise RuntimeError('unexpected coordinate')
                if self.corrupt:
                    value = (17, 18, 19, 255)
                for index, channel in enumerate(value):
                    args[-1][index] = channel
            if name in self.BOOL:
                return 1
            return None
        return call


class AcceptanceTests(unittest.TestCase):
    def run_fake(self, api=None, frames=1):
        api = api or FakeAPI()
        events = []
        m.Harness(api, lambda **event: events.append(event)).run(frames)
        return api, events

    def failure(self, api, pattern, frames=1):
        events = []
        with self.assertRaisesRegex(m.AcceptanceError, pattern):
            m.Harness(api, lambda **event: events.append(event)).run(frames)
        self.assertFalse(any(event.get('result') == 'PASS' for event in events))
        self.assertEqual(api.calls[-1], 'eglReleaseThread')
        return events

    def test_syntax_ast_asserts_free_and_import_is_inert(self):
        for path in (SOURCE, Path(__file__)):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            self.assertFalse(any(isinstance(node, ast.Assert) for node in ast.walk(tree)))
            compile(tree, str(path), 'exec')
        with patch.object(c, 'CDLL', side_effect=RuntimeError('must not load')) as load:
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
        load.assert_not_called()

    def test_cli_gate_before_api_factory(self):
        with patch.object(m, 'NativeAPI') as unused:
            def forbidden():
                self.fail('factory called without opt-in')
            with self.assertRaises(SystemExit) as result, patch('sys.stderr', new=io.StringIO()):
                m.main([], api_factory=forbidden)
            self.assertEqual(result.exception.code, 2)
            unused.assert_not_called()

    def test_cli_default_one_frame_and_opt_in_with_cpu_injection(self):
        api = FakeAPI()
        events = []
        status = m.main(['--allow-gpu-execution'], api_factory=lambda: api,
                        emit=lambda **event: events.append(event))
        self.assertEqual(status, 0)
        self.assertEqual(events[-1]['frames'], 1)
        self.assertEqual(len(api.reads), 2)

    def test_frame_bounds_at_cli_and_production_entry(self):
        for frames in (0, -1, 33, True, 1.5, '2'):
            with self.subTest(frames=frames):
                api = FakeAPI()
                with self.assertRaises(m.AcceptanceError):
                    self.run_fake(api, frames)
                self.assertEqual(api.calls, [])
        for text in ('0', '-1', '33', '1.5', 'many'):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                m.frame_count(text)
        self.assertEqual(m.validate_frames(1), 1)
        self.assertEqual(m.frame_count('32'), 32)
        for text in ('0', '33'):
            with self.subTest(cli=text), self.assertRaises(SystemExit), patch('sys.stderr', new=io.StringIO()):
                m.main(['--allow-gpu-execution', '--frames', text],
                       api_factory=lambda: self.fail('invalid CLI reached factory'))

    def test_maximum_frames_alternation_and_coordinates(self):
        api, events = self.run_fake(frames=32)
        self.assertEqual(len(api.reads), 64)
        self.assertEqual(events[-1]['result'], 'PASS')
        self.assertEqual(events[-2], {'stage': 'cleanup', 'result': 'ok'})
        for frame in range(32):
            expected = (((255, 0, 0, 255), (0, 0, 255, 255)) if frame % 2 == 0 else
                        ((0, 255, 0, 255), (255, 0, 255, 255)))
            self.assertEqual(m.palette(frame), expected)
            pair = api.reads[2 * frame:2 * frame + 2]
            self.assertEqual([row[:2] for row in pair], [(32, 32), (0, 63)])
            self.assertEqual(pair[0][2:], tuple(tuple(v / 255 for v in color) for color in expected))

    def test_stale_second_frame_rejected(self):
        api = FakeAPI(stale=True)
        self.failure(api, r'frame/1/pixel/32,32', frames=2)
        self.assertEqual(len(api.reads), 3)
        self.assertIn('glDeleteBuffers', api.calls)

    def test_bad_pixel_rejected(self):
        self.failure(FakeAPI(corrupt=True), r'frame/0/pixel/32,32')

    def test_exact_dimensions_and_fbo(self):
        for dimensions in ((63, 64), (64, 65), (0, 0)):
            api = FakeAPI(dimensions=dimensions)
            self.failure(api, 'dimensions: expected 64x64')
            self.assertNotIn('glCreateShader', api.calls)
        api = FakeAPI(fbo=0x8CD6)
        self.failure(api, 'default-fbo: incomplete')
        self.assertNotIn('glCreateShader', api.calls)

    def test_renderer_identity_minimum_and_full_strings(self):
        long_renderer = b'ZiNk GFXSTREAM MALI ' + b'identity-' * 1000
        _, events = self.run_fake(FakeAPI(renderer=long_renderer))
        identity = next(event for event in events if event.get('stage') == 'identity')
        self.assertEqual(identity['GL_RENDERER'], long_renderer.decode())
        self.assertEqual(identity['physical_device_identity'], 'not independently established')
        for renderer in (b'zink gfxstream', b'zink Mali', b'gfxstream Mali', b'llvmpipe'):
            api = FakeAPI(renderer=renderer)
            self.failure(api, 'identity/renderer')
            self.assertNotIn('glCreateShader', api.calls)

    def test_compile_and_link_logs_and_cleanup(self):
        for api, stage in ((FakeAPI(shader_ok=False), 'vertex'),
                           (FakeAPI(link_ok=False), 'program')):
            with self.subTest(stage=stage):
                events = self.failure(api, stage + ': compile/link failed')
                log = next(event for event in events if event.get('stage') == stage)
                self.assertEqual(log['log'], 'CPU compiler/linker log')
                self.assertIn('glDeleteShader', api.calls)
        api, events = self.run_fake()
        self.assertEqual([e['stage'] for e in events if 'log' in e], ['vertex', 'fragment', 'program'])

    def test_partial_acquisition_teardown(self):
        cases = (
            ('platform_display', []),
            ('eglInitialize', ['eglTerminate']),
            ('eglChooseConfig', ['eglTerminate']),
            ('eglCreatePbufferSurface', ['eglTerminate']),
            ('eglCreateContext', ['eglDestroySurface', 'eglTerminate']),
            ('eglMakeCurrent', ['eglDestroyContext', 'eglDestroySurface', 'eglTerminate']),
        )
        for failure, expected in cases:
            with self.subTest(failure=failure):
                api = FakeAPI(fail=failure)
                self.failure(api, 'display|initialize|choose-config|surface|context|make-current')
                released = [name for name in api.calls if name in
                            ('eglDestroyContext', 'eglDestroySurface', 'eglTerminate')]
                self.assertEqual(released, expected)

    def test_null_handles_fail_and_cleanup_prior_resources(self):
        for name in ('platform_display', 'eglCreatePbufferSurface', 'eglCreateContext',
                     'glCreateShader', 'glCreateProgram'):
            with self.subTest(name=name):
                api = FakeAPI(null=name)
                self.failure(api, 'display|surface|context|vertex/create|program/create')
                self.assertNotIn('glReadPixels', api.calls)

    def test_immediate_gl_errors_abort_before_readback(self):
        for function, stage in (('glBindFramebuffer', 'default-fbo/bind'),
                                ('glCompileShader', 'vertex/compile'),
                                ('glLinkProgram', 'program/link'),
                                ('glBufferData', 'buffer/data'),
                                ('glDrawArrays', 'frame/0/draw')):
            with self.subTest(function=function):
                api = FakeAPI(gl_error=function)
                self.failure(api, stage + ': GL error')
                self.assertNotIn('glReadPixels', api.calls)

    def test_handles_retained_when_creation_sets_error(self):
        api = FakeAPI(egl_error='eglCreateContext')
        self.failure(api, 'context: creation failed')
        self.assertIn('eglDestroyContext', api.calls)
        api = FakeAPI(gl_error='glCreateShader')
        self.failure(api, 'vertex/create')
        self.assertIn('glDeleteShader', api.calls)
        api = FakeAPI(gl_error='glGenBuffers')
        self.failure(api, 'buffer/generate')
        self.assertIn('glDeleteBuffers', api.calls)

    def test_egl_error_with_true_result_rejected(self):
        self.failure(FakeAPI(egl_error='eglQuerySurface'), 'dimensions/width')
        api = FakeAPI(egl_error='eglMakeCurrent')
        self.failure(api, 'make-current')
        self.assertEqual(api.calls.count('eglMakeCurrent'), 2)

    def test_cleanup_order_checked_before_final_pass(self):
        api, events = self.run_fake()
        tail = api.calls[api.calls.index('glDisableVertexAttribArray'):]
        self.assertEqual(tail, [
            'glDisableVertexAttribArray', 'glBindBuffer', 'glDeleteBuffers',
            'glUseProgram', 'glDeleteProgram', 'glDeleteShader', 'glDeleteShader',
            'eglMakeCurrent', 'eglDestroyContext', 'eglDestroySurface',
            'eglTerminate', 'eglReleaseThread'])
        self.assertEqual(events[-1]['result'], 'PASS')

    def test_each_cleanup_error_suppresses_pass_and_continues(self):
        for function in ('glDisableVertexAttribArray', 'glBindBuffer', 'glDeleteBuffers',
                         'glUseProgram', 'glDeleteProgram', 'glDeleteShader'):
            # Inject only once cleanup starts, avoiding setup errors on shared symbols.
            api = FakeAPI()
            original = api.__getattr__('glDisableVertexAttribArray')
            def start_cleanup(*args, api=api, function=function, original=original):
                api.gl_error = function
                return original(*args)
            api.glDisableVertexAttribArray = start_cleanup
            with self.subTest(function=function):
                self.failure(api, 'cleanup/')
                self.assertIn('eglTerminate', api.calls)
        for function in ('release-current', 'eglDestroyContext', 'eglDestroySurface',
                         'eglTerminate', 'eglReleaseThread'):
            with self.subTest(function=function):
                self.failure(FakeAPI(fail=function), 'cleanup/')

    def test_primary_error_and_cleanup_errors_both_preserved(self):
        api = FakeAPI(corrupt=True, fail='eglDestroyContext')
        self.failure(api, r'frame/0/pixel/32,32.*additionally.*cleanup/destroy-context')
        self.assertIn('eglDestroySurface', api.calls)
        self.assertIn('eglTerminate', api.calls)

    def test_unexpected_exception_still_tears_down(self):
        api = FakeAPI()
        def broken(*args):
            raise ValueError('injected Python failure')
        api.glDrawArrays = broken
        events = []
        with self.assertRaisesRegex(ValueError, 'injected Python failure'):
            m.Harness(api, lambda **event: events.append(event)).run()
        self.assertEqual(api.calls[-1], 'eglReleaseThread')
        self.assertFalse(any(event.get('result') == 'PASS' for event in events))

    def test_cli_failure_exit_status(self):
        events = []
        status = m.main(['--allow-gpu-execution'], api_factory=lambda: FakeAPI(corrupt=True),
                        emit=lambda **event: events.append(event))
        self.assertEqual(status, 1)
        self.assertEqual(events[-1]['result'], 'FAIL')
        self.assertIn('frame/0/pixel', events[-1]['error'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    options = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AcceptanceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = {
        'scope': 'CPU-only production Harness with injected raw EGL/GL stub',
        'native_library_loading': 'forbidden by patched ctypes.CDLL',
        'gpu_execution': False,
        'device_execution': False,
        'hardware_acceptance_goal': 'UNACHIEVED',
        'tests_run': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'successful': result.wasSuccessful(),
        'optimization_enabled': not __debug__,
        'limitations': [
            'No real EGL/GLES ABI, driver, synchronization or pixel validation',
            'No physical GPU identity established',
            'Default one-frame run does not test stale-frame alternation',
        ],
    }
    options.evidence.parent.mkdir(parents=True, exist_ok=True)
    options.evidence.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
