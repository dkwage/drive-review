import datetime as dt
import os
import plistlib
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

CATEGORIES = {
    '영상': {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.mpeg', '.mpg', '.webm', '.m4v',
           '.3gp', '.3g2', '.asf', '.flv', '.mts', '.m2ts', '.mxf', '.vob', '.ogv', '.divx'},
    '사진': {'.jpg', '.jpeg', '.jpe', '.jif', '.jfif', '.png', '.apng', '.gif', '.bmp', '.webp',
           '.heic', '.heif', '.avif', '.tif', '.tiff', '.ico', '.svg',
           '.raw', '.arw', '.srf', '.sr2', '.cr2', '.cr3', '.crw', '.nef', '.nrw', '.dng',
           '.raf', '.rw2', '.orf', '.pef', '.ptx', '.srw', '.x3f', '.3fr', '.iiq', '.mos', '.rwl', '.dcr', '.kdc'},
    '문서': {'.pdf', '.doc', '.docx', '.docm', '.dot', '.dotx', '.xls', '.xlsx', '.xlsm', '.xlsb',
           '.ppt', '.pptx', '.pptm', '.pps', '.ppsx', '.hwp', '.hwpx', '.hwt', '.txt', '.csv',
           '.tsv', '.odt', '.ods', '.odp', '.rtf', '.pages', '.numbers', '.key', '.md', '.epub'},
    '음악': {'.mp3', '.wav', '.flac', '.aac', '.m4a', '.ogg', '.wma', '.aiff', '.aif', '.alac',
           '.ape', '.opus', '.mid', '.midi', '.amr', '.mka', '.dsf', '.dff'},
    '압축': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.tbz2', '.txz', '.zst',
           '.cab', '.iso', '.alz', '.egg'},
    '디자인·편집': {'.psd', '.psb', '.psdt', '.ai', '.ait', '.eps', '.indd', '.idml', '.indt', '.incx',
               '.aep', '.aepx', '.aet', '.prproj', '.prprojx', '.mogrt', '.sesx', '.fla', '.xd', '.lrcat'},
}
HEADERS = ['분류', '파일명', '확장자', '상대 경로', '크기(바이트)', '수정 시간']


def category(name):
    extension = Path(name).suffix.lower()
    return next((kind for kind, extensions in CATEGORIES.items() if extension in extensions), '기타')


def usb_drives():
    if sys.platform == 'darwin':
        result = subprocess.run(['diskutil', 'list', '-plist', 'external'],
                                capture_output=True, timeout=20, check=True)
        drives = set()
        for disk in plistlib.loads(result.stdout)['AllDisks']:
            info = subprocess.run(['diskutil', 'info', '-plist', disk],
                                  capture_output=True, timeout=20)
            if info.returncode:
                continue  # A disk can disappear during enumeration.
            mount = plistlib.loads(info.stdout).get('MountPoint')
            if mount and os.path.isdir(mount):
                drives.add(mount)
        return drives
    # Get-Disk includes USB external hard drives reported as fixed disks.
    script = "Get-Disk | Where-Object BusType -eq 'USB' | Get-Partition | Where-Object DriveLetter | ForEach-Object { $_.DriveLetter }"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'USB 드라이브 조회 실패')
    return {line.strip().upper() + ':\\' for line in result.stdout.splitlines()
            if len(line.strip()) == 1 and line.strip().isascii() and line.strip().isalpha()}


def excel_text(value):
    # Force filenames beginning with = to remain text, not spreadsheet formulas.
    return ILLEGAL_CHARACTERS_RE.sub('', str(value))[:32767]


def drive_name(root):
    if sys.platform == 'win32':
        import ctypes
        name = ctypes.create_unicode_buffer(256)
        if ctypes.windll.kernel32.GetVolumeInformationW(root, name, len(name), None, None, None, None, 0):
            return name.value or root[:1]
        return root[:1]
    return os.path.basename(os.path.normpath(root)) or '드라이브'


def sheet_name(root, existing):
    name = re.sub(r"[\\/*?:\[\]\x00-\x1f]", '_', drive_name(root)).strip(" '") or '드라이브'
    existing = {title.casefold() for title in existing}
    title = name[:31].strip(" '") or '드라이브'
    index = 2
    while title.casefold() in existing:
        suffix = f' ({index})'
        title = name[:31 - len(suffix)].rstrip(" '") + suffix
        index += 1
    return title


def scan_drive(root, output, stop, report):
    errors = []
    rows = []
    def onerror(error):
        errors.append(str(error))
    for folder, dirs, files in os.walk(root, onerror=onerror, followlinks=False):
        if stop.is_set() or not os.path.exists(root):
            raise InterruptedError('연결 해제 또는 종료로 스캔이 취소되었습니다.')
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
        for name in files:
            if stop.is_set() or not os.path.exists(root):
                raise InterruptedError('연결 해제 또는 종료로 스캔이 취소되었습니다.')
            path = os.path.join(folder, name)
            try:
                if os.path.islink(path):
                    continue
                info = os.stat(path)
                rows.append((category(name), name, Path(name).suffix.lower(),
                             os.path.relpath(path, root), info.st_size,
                             dt.datetime.fromtimestamp(info.st_mtime).strftime('%Y-%m-%d %H:%M:%S')))
            except OSError as error:
                errors.append(str(error))
    if not os.path.exists(root) or stop.is_set():
        raise InterruptedError('연결 해제 또는 종료로 스캔이 취소되었습니다.')
    if len(rows) > 1048575:
        raise ValueError('파일 수가 엑셀 시트의 최대 행 수를 초과합니다.')
    book = load_workbook(output) if os.path.exists(output) else Workbook()
    if not os.path.exists(output):
        book.remove(book.active)
    title = sheet_name(root, book.sheetnames)
    sheet = book.create_sheet(title)
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([excel_text(v) if isinstance(v, str) else v for v in row])
        for cell in sheet[sheet.max_row]:
            if cell.data_type == 'f':
                cell.data_type = 's'
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in zip('ABCDEF', (12, 35, 12, 65, 18, 22)):
        sheet.column_dimensions[column].width = width
    directory = os.path.dirname(os.path.abspath(output))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', dir=directory, delete=False) as temp:
            temp_path = temp.name
        book.save(temp_path)
        if stop.is_set() or not os.path.exists(root):
            raise InterruptedError('연결 해제 또는 종료로 스캔이 취소되었습니다.')
        os.replace(temp_path, output)
    finally:
        book.close()
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    report(f'{root} 저장 완료: {len(rows):,}개 파일, 시트 {sheet.title}')
    if errors:
        report(f'읽지 못한 항목 {len(errors)}개 (일부 누락): ' + '; '.join(errors[:3]))


class App:
    def __init__(self, window):
        self.window = window
        window.title('USB 파일 목록 수집기')
        window.geometry('760x450')
        self.output = tk.StringVar(value=str(Path.home() / 'Documents' / 'USB_파일목록.xlsx'))
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='저장할 엑셀 파일').pack(anchor='w')
        line = ttk.Frame(frame)
        line.pack(fill='x', pady=6)
        self.entry = ttk.Entry(line, textvariable=self.output)
        self.entry.pack(side='left', fill='x', expand=True)
        self.browse = ttk.Button(line, text='찾아보기', command=self.choose)
        self.browse.pack(side='left', padx=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=6)
        self.start_button = ttk.Button(buttons, text='시작', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(buttons, text='종료', command=self.stop_monitoring, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        self.status = ttk.Label(frame, text='대기 중')
        self.status.pack(anchor='w', pady=6)
        self.log = tk.Text(frame, height=15, state='disabled', wrap='word')
        self.log.pack(fill='both', expand=True)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(100, self.process_events)

    def choose(self):
        path = filedialog.asksaveasfilename(defaultextension='.xlsx', filetypes=[('Excel 통합 문서', '*.xlsx')], initialfile='USB_파일목록.xlsx')
        if path:
            self.output.set(path)

    def start(self):
        path = self.output.get().strip()
        if not path or not path.lower().endswith('.xlsx') or not os.path.isdir(os.path.dirname(os.path.abspath(path))):
            messagebox.showerror('저장 경로 오류', '존재하는 폴더의 .xlsx 파일을 선택하세요.')
            return
        self.stop.clear()
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')
        self.entry.config(state='disabled')
        self.browse.config(state='disabled')
        self.status.config(text='USB 연결 감시 중')
        self.worker = threading.Thread(target=self.monitor, args=(os.path.abspath(path),), daemon=True)
        self.worker.start()

    def monitor(self, path):
        seen = set()
        try:
            while not self.stop.is_set():
                try:
                    current = usb_drives()
                    seen.intersection_update(current)
                    for drive in sorted(current - seen):
                        if self.stop.is_set():
                            break
                        seen.add(drive)
                        self.events.put(('log', f'{drive} 연결됨: 파일 목록을 읽는 중'))
                        try:
                            scan_drive(drive, path, self.stop, lambda text: self.events.put(('log', text)))
                        except Exception as error:
                            self.events.put(('log', f'{drive} 저장 실패: {error}'))
                    self.stop.wait(2)
                except Exception as error:
                    self.events.put(('log', f'USB 감지 오류: {error}'))
                    self.stop.wait(5)
        finally:
            self.events.put(('stopped', '감시 종료'))

    def stop_monitoring(self):
        self.stop.set()
        self.stop_button.config(state='disabled')
        self.status.config(text='종료 중…')

    def process_events(self):
        try:
            while True:
                kind, message = self.events.get_nowait()
                self.log.config(state='normal')
                self.log.insert('end', f'[{dt.datetime.now():%H:%M:%S}] {message}\n')
                self.log.see('end')
                self.log.config(state='disabled')
                if kind == 'stopped':
                    self.worker = None
                    self.status.config(text='대기 중')
                    self.start_button.config(state='normal')
                    self.entry.config(state='normal')
                    self.browse.config(state='normal')
        except queue.Empty:
            pass
        self.window.after(100, self.process_events)

    def close(self):
        if self.worker is not None:
            self.stop_monitoring()
            self.window.after(100, self.close)
        else:
            self.window.destroy()


if __name__ == '__main__':
    if sys.platform not in ('win32', 'darwin'):
        raise SystemExit('이 프로그램은 Windows 또는 macOS에서 실행해야 합니다.')
    App(tk.Tk()).window.mainloop()
