"""Private native-JSON FIFO transport to lossless archives, never a netlist edit."""
import gzip
import hashlib
import os
from pathlib import Path
import stat
import threading


class JSONPipes:
    def __init__(self, directory, names):
        self.done=threading.Event();self.rows=[];self.jobs=[]
        for name in names:
            path=Path(directory)/name;packed=path.with_name(path.name+'.gz')
            if path.exists() or packed.exists():raise ValueError('Fresh JSON FIFO and archive required')
            os.mkfifo(path,0o600)
            descriptor=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
            info=os.fstat(descriptor)
            row=dict(logical=str(path),archive=str(packed),fifo_inode=info.st_ino,bytes=0,sha256=None,error=None)
            self.rows.append(row)
            thread=threading.Thread(target=self._pump,args=(descriptor,row),daemon=True)
            self.jobs.append(thread);thread.start()

    def _pump(self, descriptor, row):
        digest=hashlib.sha256();count=0;seen=False
        try:
            with open(row['archive'],'xb') as raw:
                with gzip.GzipFile(filename='',mode='wb',fileobj=raw,compresslevel=1,mtime=0) as target:
                    while True:
                        try:block=os.read(descriptor,1024*1024)
                        except BlockingIOError:block=None
                        if block:
                            seen=True;count+=len(block);digest.update(block);target.write(block)
                        elif block==b'' and (seen or self.done.is_set()):break
                        else:self.done.wait(0.01)
                raw.flush();os.fsync(raw.fileno())
            row.update(bytes=count,sha256=digest.hexdigest())
        except BaseException as error:row['error']=repr(error)
        finally:os.close(descriptor)

    def finish(self, code):
        self.done.set()
        for job in self.jobs:job.join()
        for row in self.rows:
            path=Path(row['logical']);info=path.lstat()
            if not stat.S_ISFIFO(info.st_mode) or info.st_ino!=row['fifo_inode']:raise ValueError('Fresh JSON FIFO identity changed')
            path.unlink()  # only the fresh empty FIFO; all emitted bytes stay in .gz
        if code==0 and any(r['error'] or not r['bytes'] for r in self.rows):
            raise RuntimeError('Native JSON FIFO producer did not complete every expected output: '+repr(self.rows))
        return self.rows
