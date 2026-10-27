"""Private phase logs: no command lines, child output, or subscription bodies."""
import contextlib
import datetime
import os
from pathlib import Path
import re
import threading
import time


def redact(message):
    message = re.sub(r'-----BEGIN [^-]+-----.*?-----END [^-]+-----',
                     '[PRIVATE DATA]', str(message), flags=re.S)
    message = re.sub(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"\'<>]+', '[URL]', message)
    message = re.sub(r'(?i)((?:password|passwd|token|secret|private[_ -]?key|uuid)\s*[=:]\s*)[^\s,;]+',
                     r'\1[REDACTED]', message)
    message = re.sub(r'(?<![\w])[A-Za-z0-9_=-]{32,}(?![\w])', '[REDACTED]', message)
    return message


class InstallLog:
    def __init__(self, interval=15):
        self.interval = interval
        self.stream = None
        self.path = None
        self.pending = []
        self.lock = threading.RLock()

    def emit(self, status, message):
        stamp = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        row = f'{stamp} [{status}] {redact(message)}'
        with self.lock:
            try:
                print(row, flush=True)
            except OSError:
                pass  # A lost terminal must not interrupt a transaction.
            if self.stream:
                try:
                    self.stream.write(row + '\n')
                    self.stream.flush()
                except OSError:
                    self.close()
                    try:
                        print('[日志警告] 持久日志写入失败，请检查磁盘空间；安装状态以 status 为准。', flush=True)
                    except OSError:
                        pass
            else:
                self.pending.append(row)

    def attach(self, path):
        self.path = Path(path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, 'w', encoding='utf-8')
        with self.lock:
            self.stream.write('\n'.join(self.pending) + '\n')
            self.stream.flush()
            self.pending.clear()

    @contextlib.contextmanager
    def step(self, number, label):
        started = time.monotonic()
        title = f'{number}/8 {label}'
        self.emit('开始', title)
        done = threading.Event()

        def heartbeat():
            while not done.wait(self.interval):
                self.emit('进行中', f'{title}；已等待 {time.monotonic() - started:.0f} 秒')

        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield
        except BaseException as error:
            self.emit('失败', f'{title}；{type(error).__name__}: {error}')
            raise
        else:
            self.emit('完成', f'{title}；耗时 {time.monotonic() - started:.1f} 秒')
        finally:
            done.set()
            worker.join(timeout=1)

    def close(self):
        with self.lock:
            if self.stream:
                try:
                    self.stream.close()
                except OSError:
                    pass
                self.stream = None
