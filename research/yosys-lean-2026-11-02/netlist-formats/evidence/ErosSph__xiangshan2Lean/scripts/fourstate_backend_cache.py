"""Private artifact page-cache advice; no cgroup or global cache policy changes."""
import os
from pathlib import Path
import stat
import threading


def advise(path, run):
    path=Path(os.path.abspath(path));run=Path(run).resolve(strict=True)
    relative=path.relative_to(run/'reports')
    current=run/'reports'
    for part in relative.parts:
        current=current/part
        if stat.S_ISLNK(current.lstat().st_mode):raise ValueError('Cache advice refuses symlinks')
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
    try:
        before=os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_dev!=run.stat().st_dev or before.st_nlink!=1:
            raise ValueError('Cache advice requires an owned same-device single-link artifact')
        # Dirty pages cannot be discarded. This writes existing dirty bytes to
        # their backing file; it does not rewrite or truncate the artifact.
        os.fdatasync(descriptor)
        os.posix_fadvise(descriptor,0,0,os.POSIX_FADV_DONTNEED)
        after=os.fstat(descriptor)
        if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):raise ValueError('Cache advice identity changed')
        return dict(path=str(path),bytes=after.st_size,inode=after.st_ino,operation='fdatasync + POSIX_FADV_DONTNEED')
    finally:os.close(descriptor)


class ArtifactCache:
    def __init__(self, run, out):
        self.run=Path(run);self.out=Path(out);self.stop=threading.Event();self.error=None
        self.counts={};self.thread=None

    def start(self):
        def watch():
            while not self.stop.is_set():
                try:
                    for path in self.out.glob('translation/*/artifacts/*'):
                        if path.name not in {'design.v','before.il','unconstrained.il','before.json','unconstrained.json','named.json','lowered.json','exposed.json'} and not path.name.endswith('.json.gz'):continue
                        try:
                            if stat.S_ISFIFO(path.lstat().st_mode):continue
                            advise(path,self.run)
                            self.counts[str(path)]=self.counts.get(str(path),0)+1
                        except FileNotFoundError:pass  # verified archive can replace a fresh raw file
                except BaseException as error:
                    self.error=error;return
                self.stop.wait(1)
        self.thread=threading.Thread(target=watch,daemon=True);self.thread.start()

    def finish(self):
        self.stop.set()
        if self.thread:self.thread.join()
        if self.error:raise RuntimeError('Private cache adviser failed: '+str(self.error)) from self.error
