#!/usr/bin/env python3
"""Opt-in EGL/GLES diagnostic. Importing does not load graphics libraries."""
import argparse
import ctypes as c
import json
import sys

SIZE = 64
MAX_FRAMES = 32
EGL_SUCCESS = 0x3000
EGL_NONE = 0x3038
EGL_WIDTH, EGL_HEIGHT = 0x3057, 0x3056
GL_FRAMEBUFFER = 0x8D40
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
SAMPLES = ((32, 32), (0, 63))  # lower-left origin: triangle interior, background
PALETTES = (
    ((255, 0, 0, 255), (0, 0, 255, 255)),
    ((0, 255, 0, 255), (255, 0, 255, 255)),
)
VERTEX = b'attribute vec2 pos; void main(){gl_Position=vec4(pos,0.0,1.0);}'
FRAGMENT = (b'precision mediump float; uniform vec4 color; '
            b'void main(){gl_FragColor=color;}')


class AcceptanceError(RuntimeError):
    pass


def require(condition, stage, detail):
    if not condition:
        raise AcceptanceError(f'{stage}: {detail}')


def validate_frames(frames):
    require(type(frames) is int and 1 <= frames <= MAX_FRAMES,
            'frames', f'must be an integer in [1, {MAX_FRAMES}]')
    return frames


def frame_count(text):
    try:
        return validate_frames(int(text))
    except (ValueError, AcceptanceError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def palette(frame):
    return PALETTES[frame % 2]


def emit_record(**fields):
    print(json.dumps(fields, ensure_ascii=True), flush=True)


def bind(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, args
    return fn


class NativeAPI:
    """Raw ctypes functions; constructed only after the explicit CLI gate."""
    def __init__(self):
        self.egl = c.CDLL('libEGL.so.1')
        self.gl = c.CDLL('libGLESv2.so.2')
        p, i, u, f, s = c.c_void_p, c.c_int, c.c_uint, c.c_float, c.c_char_p
        ip, up = c.POINTER(i), c.POINTER(u)
        egl_specs = {
            'eglGetError': (u,), 'eglGetProcAddress': (p, s),
            'eglInitialize': (u, p, ip, ip), 'eglBindAPI': (u, u),
            'eglChooseConfig': (u, p, ip, c.POINTER(p), i, ip),
            'eglCreatePbufferSurface': (p, p, p, ip),
            'eglCreateContext': (p, p, p, p, ip),
            'eglMakeCurrent': (u, p, p, p, p),
            'eglQuerySurface': (u, p, p, i, ip),
            'eglQueryString': (s, p, i),
            'eglDestroyContext': (u, p, p), 'eglDestroySurface': (u, p, p),
            'eglTerminate': (u, p), 'eglReleaseThread': (u,),
        }
        gl_specs = {
            'glGetError': (u,), 'glGetString': (s, u),
            'glBindFramebuffer': (None, u, u), 'glCheckFramebufferStatus': (u, u),
            'glCreateShader': (u, u),
            'glShaderSource': (None, u, i, c.POINTER(s), p),
            'glCompileShader': (None, u), 'glGetShaderiv': (None, u, u, ip),
            'glGetShaderInfoLog': (None, u, i, ip, p),
            'glCreateProgram': (u,), 'glAttachShader': (None, u, u),
            'glBindAttribLocation': (None, u, u, s), 'glLinkProgram': (None, u),
            'glGetProgramiv': (None, u, u, ip),
            'glGetProgramInfoLog': (None, u, i, ip, p),
            'glUseProgram': (None, u), 'glGetUniformLocation': (i, u, s),
            'glUniform4f': (None, i, f, f, f, f),
            'glGenBuffers': (None, i, up), 'glBindBuffer': (None, u, u),
            'glBufferData': (None, u, c.c_ssize_t, p, u),
            'glVertexAttribPointer': (None, u, i, u, c.c_ubyte, i, p),
            'glEnableVertexAttribArray': (None, u),
            'glDisableVertexAttribArray': (None, u),
            'glViewport': (None, i, i, i, i), 'glDisable': (None, u),
            'glClearColor': (None, f, f, f, f), 'glClear': (None, u),
            'glDrawArrays': (None, u, i, i), 'glFinish': (None,),
            'glPixelStorei': (None, u, i),
            'glReadPixels': (None, i, i, i, i, u, u, p),
            'glDeleteBuffers': (None, i, up), 'glDeleteProgram': (None, u),
            'glDeleteShader': (None, u),
        }
        for name, spec in egl_specs.items():
            setattr(self, name, bind(self.egl, name, *spec))
        for name, spec in gl_specs.items():
            setattr(self, name, bind(self.gl, name, *spec))

    def platform_display(self):
        address = self.eglGetProcAddress(b'eglGetPlatformDisplayEXT')
        require(address, 'display/proc', 'eglGetPlatformDisplayEXT unavailable')
        fn = c.CFUNCTYPE(c.c_void_p, c.c_uint, c.c_void_p,
                         c.POINTER(c.c_int))(address)
        return fn(0x31DD, None, None)  # EGL_PLATFORM_SURFACELESS_MESA


class Harness:
    """Production lifecycle and validation; tests inject raw CPU EGL/GL fakes."""
    def __init__(self, api, emit=emit_record):
        self.api, self.emit = api, emit
        self.display = self.surface = self.context = None
        self.initialize_attempted = self.current = False
        self.shaders = []
        self.program = 0
        self.buffer = c.c_uint()

    def egl_call(self, stage, fn, *args):
        result = fn(*args)
        error = self.api.eglGetError()
        require(bool(result) and error == EGL_SUCCESS, stage,
                f'EGL result={result!r} error=0x{error:04x}')
        return result

    def gl_call(self, stage, fn, *args):
        result = fn(*args)
        error = self.api.glGetError()
        require(error == 0, stage, f'GL error=0x{error:04x}')
        return result

    def open(self):
        a = self.api
        # Assign acquired handles BEFORE checking errors so partial success is cleaned.
        self.display = a.platform_display()
        error = a.eglGetError()
        require(self.display and error == EGL_SUCCESS, 'display',
                f'no display or EGL error=0x{error:04x}')
        major, minor = c.c_int(), c.c_int()
        self.initialize_attempted = True
        self.egl_call('initialize', a.eglInitialize, self.display,
                      c.byref(major), c.byref(minor))
        self.egl_call('bind-api', a.eglBindAPI, 0x30A0)
        attrs = (c.c_int * 17)(0x3033, 1, 0x3040, 4, 0x3024, 8, 0x3023, 8,
                               0x3022, 8, 0x3021, 8, 0x3032, 0, 0x3025, 0, EGL_NONE)
        config, count = c.c_void_p(), c.c_int()
        self.egl_call('choose-config', a.eglChooseConfig, self.display, attrs,
                      c.byref(config), 1, c.byref(count))
        require(count.value > 0 and config.value, 'choose-config', 'no config')
        self.surface = a.eglCreatePbufferSurface(
            self.display, config,
            (c.c_int * 5)(EGL_WIDTH, SIZE, EGL_HEIGHT, SIZE, EGL_NONE))
        error = a.eglGetError()
        require(self.surface and error == EGL_SUCCESS, 'surface',
                f'creation failed; EGL error=0x{error:04x}')
        self.context = a.eglCreateContext(
            self.display, config, None, (c.c_int * 3)(0x3098, 2, EGL_NONE))
        error = a.eglGetError()
        require(self.context and error == EGL_SUCCESS, 'context',
                f'creation failed; EGL error=0x{error:04x}')
        result = a.eglMakeCurrent(self.display, self.surface, self.surface, self.context)
        self.current = bool(result)
        error = a.eglGetError()
        require(self.current and error == EGL_SUCCESS, 'make-current',
                f'failed; EGL error=0x{error:04x}')
        dimensions = []
        for key, name in ((EGL_WIDTH, 'width'), (EGL_HEIGHT, 'height')):
            value = c.c_int()
            self.egl_call('dimensions/' + name, a.eglQuerySurface,
                          self.display, self.surface, key, c.byref(value))
            dimensions.append(value.value)
        self.emit(stage='dimensions', width=dimensions[0], height=dimensions[1])
        require(dimensions == [SIZE, SIZE], 'dimensions', f'expected 64x64, got {dimensions}')
        self.gl_call('default-fbo/bind', a.glBindFramebuffer, GL_FRAMEBUFFER, 0)
        status = self.gl_call('default-fbo/status', a.glCheckFramebufferStatus, GL_FRAMEBUFFER)
        require(status == GL_FRAMEBUFFER_COMPLETE, 'default-fbo', f'incomplete: 0x{status:04x}')

    def identity(self):
        a = self.api
        identity = {}
        for name, key in (('EGL_VENDOR', 0x3053), ('EGL_VERSION', 0x3054),
                          ('EGL_CLIENT_APIS', 0x308D), ('EGL_EXTENSIONS', 0x3055)):
            raw = self.egl_call('identity/' + name, a.eglQueryString, self.display, key)
            identity[name] = raw.decode('utf-8', errors='backslashreplace')
        for name, key in (('GL_VENDOR', 0x1F00), ('GL_RENDERER', 0x1F01),
                          ('GL_VERSION', 0x1F02), ('GL_SHADING_LANGUAGE_VERSION', 0x8B8C),
                          ('GL_EXTENSIONS', 0x1F03)):
            raw = self.gl_call('identity/' + name, a.glGetString, key)
            require(raw is not None, 'identity/' + name, 'null string')
            identity[name] = raw.decode('utf-8', errors='backslashreplace')
        self.emit(stage='identity', **identity,
                  physical_device_identity='not independently established')
        renderer = identity['GL_RENDERER'].lower()
        missing = [part for part in ('zink', 'gfxstream', 'mali') if part not in renderer]
        require(not missing, 'identity/renderer', f'missing required substrings: {missing}')

    def object_status_log(self, handle, program, stage):
        a = self.api
        query = a.glGetProgramiv if program else a.glGetShaderiv
        get_log = a.glGetProgramInfoLog if program else a.glGetShaderInfoLog
        status, length = c.c_int(), c.c_int()
        self.gl_call(stage + '/status', query, handle, 0x8B82 if program else 0x8B81,
                     c.byref(status))
        self.gl_call(stage + '/log-length', query, handle, 0x8B84, c.byref(length))
        require(0 <= length.value <= 1024 * 1024, stage, f'invalid log length {length.value}')
        log = c.create_string_buffer(max(1, length.value))
        written = c.c_int()
        self.gl_call(stage + '/log', get_log, handle, len(log), c.byref(written), log)
        self.emit(stage=stage, status=status.value,
                  log=log.value.decode('utf-8', errors='backslashreplace'))
        require(status.value != 0, stage, 'compile/link failed (see log)')

    def shader(self, kind, source, stage):
        a = self.api
        handle = a.glCreateShader(kind)
        if handle:
            self.shaders.append(handle)
        error = a.glGetError()
        require(handle and error == 0, stage + '/create', f'handle={handle} GL=0x{error:04x}')
        text = c.c_char_p(source)
        self.gl_call(stage + '/source', a.glShaderSource, handle, 1, c.byref(text), None)
        self.gl_call(stage + '/compile', a.glCompileShader, handle)
        self.object_status_log(handle, False, stage)
        return handle

    def setup_draw(self):
        a = self.api
        vertex = self.shader(0x8B31, VERTEX, 'vertex')
        fragment = self.shader(0x8B30, FRAGMENT, 'fragment')
        self.program = a.glCreateProgram()
        error = a.glGetError()
        require(self.program and error == 0, 'program/create', f'GL=0x{error:04x}')
        for shader in (vertex, fragment):
            self.gl_call('program/attach', a.glAttachShader, self.program, shader)
        self.gl_call('program/attribute', a.glBindAttribLocation, self.program, 0, b'pos')
        self.gl_call('program/link', a.glLinkProgram, self.program)
        self.object_status_log(self.program, True, 'program')
        self.gl_call('program/use', a.glUseProgram, self.program)
        location = self.gl_call('program/uniform', a.glGetUniformLocation, self.program, b'color')
        require(location >= 0, 'program/uniform', 'color uniform missing')
        vertices = (c.c_float * 6)(-0.8, -0.8, 0.8, -0.8, 0.0, 0.8)
        self.gl_call('buffer/generate', a.glGenBuffers, 1, c.byref(self.buffer))
        require(self.buffer.value != 0, 'buffer/generate', 'zero buffer')
        self.gl_call('buffer/bind', a.glBindBuffer, 0x8892, self.buffer.value)
        self.gl_call('buffer/data', a.glBufferData, 0x8892, c.sizeof(vertices), vertices, 0x88E4)
        self.gl_call('attribute/pointer', a.glVertexAttribPointer, 0, 2, 0x1406, 0, 0, None)
        self.gl_call('attribute/enable', a.glEnableVertexAttribArray, 0)
        self.gl_call('viewport', a.glViewport, 0, 0, SIZE, SIZE)
        for cap in (0x0BD0, 0x0BE2, 0x0C11, 0x0B71, 0x0B44):
            self.gl_call('state/disable', a.glDisable, cap)  # dither/blend/scissor/depth/cull
        self.gl_call('pack-alignment', a.glPixelStorei, 0x0D05, 1)
        return location

    def draw_frame(self, frame, location):
        a = self.api
        triangle, background = palette(frame)
        stage = f'frame/{frame}'
        self.gl_call(stage + '/background', a.glClearColor, *(v / 255 for v in background))
        self.gl_call(stage + '/clear', a.glClear, 0x4000)
        self.gl_call(stage + '/triangle', a.glUniform4f, location, *(v / 255 for v in triangle))
        self.gl_call(stage + '/draw', a.glDrawArrays, 4, 0, 3)
        self.gl_call(stage + '/finish', a.glFinish)
        for (x, y), expected in zip(SAMPLES, (triangle, background)):
            pixel = (c.c_ubyte * 4)()
            self.gl_call(stage + f'/read/{x},{y}', a.glReadPixels,
                         x, y, 1, 1, 0x1908, 0x1401, pixel)
            actual = tuple(pixel)
            self.emit(stage=stage + '/pixel', coordinate=[x, y],
                      expected=expected, actual=actual)
            require(actual == expected, stage + f'/pixel/{x},{y}',
                    f'expected={expected}, actual={actual}')

    def cleanup(self):
        """Best effort all steps, retain every failure; never report PASS on failure."""
        a = self.api
        errors = []

        def attempt(stage, fn, *args, egl=False):
            try:
                if egl:
                    self.egl_call(stage, fn, *args)
                else:
                    self.gl_call(stage, fn, *args)
            except BaseException as exc:
                errors.append(f'{stage}: {exc}')

        if self.current:
            attempt('cleanup/disable-attribute', a.glDisableVertexAttribArray, 0)
            attempt('cleanup/unbind-buffer', a.glBindBuffer, 0x8892, 0)
            if self.buffer.value:
                attempt('cleanup/delete-buffer', a.glDeleteBuffers, 1, c.byref(self.buffer))
            attempt('cleanup/unuse-program', a.glUseProgram, 0)
            if self.program:
                attempt('cleanup/delete-program', a.glDeleteProgram, self.program)
            for shader in reversed(self.shaders):
                attempt('cleanup/delete-shader', a.glDeleteShader, shader)
            attempt('cleanup/release-current', a.eglMakeCurrent,
                    self.display, None, None, None, egl=True)
        if self.context:
            attempt('cleanup/destroy-context', a.eglDestroyContext, self.display, self.context, egl=True)
        if self.surface:
            attempt('cleanup/destroy-surface', a.eglDestroySurface, self.display, self.surface, egl=True)
        if self.initialize_attempted:
            attempt('cleanup/terminate', a.eglTerminate, self.display, egl=True)
        attempt('cleanup/release-thread', a.eglReleaseThread, egl=True)
        if errors:
            raise AcceptanceError('; '.join(errors))
        self.emit(stage='cleanup', result='ok')

    def run(self, frames=1):
        validate_frames(frames)
        primary = cleanup_error = None
        try:
            self.open()
            self.identity()
            location = self.setup_draw()
            for frame in range(frames):
                self.draw_frame(frame, location)
        except BaseException as exc:
            primary = exc
        finally:
            try:
                self.cleanup()
            except BaseException as exc:
                cleanup_error = exc
        if primary is not None:
            if cleanup_error is not None:
                raise AcceptanceError(f'{primary}; additionally {cleanup_error}') from primary
            raise primary
        if cleanup_error is not None:
            raise cleanup_error
        self.emit(result='PASS', frames=frames, scope='sampled offscreen pixels and checked cleanup only',
                  physical_device_identity='not independently established')


def main(argv=None, api_factory=NativeAPI, emit=emit_record):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames', type=frame_count, default=1, help='bounded frame count: 1..32')
    parser.add_argument('--allow-gpu-execution', action='store_true',
                        help='explicit execution opt-in; NOT a security boundary or user confirmation')
    options = parser.parse_args(argv)
    if not options.allow_gpu_execution:
        parser.error('GPU execution refused: --allow-gpu-execution required; obtain separate user confirmation first')
    try:
        api = api_factory()
        Harness(api, emit).run(options.frames)
    except Exception as exc:
        emit(result='FAIL', error=str(exc))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
