"""Sandstorm Map Rebuilder app: extract a map mod's paks, then hand off to the SMR menu in the SDK."""
import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from smr import __version__, __build__, core, preflight  # noqa: E402

MODIO_DIR = r'C:\Users\Public\mod.io\254\mods'
PAD = 8


def open_path(path):
    if sys.platform == 'win32':
        os.startfile(path)
    else:
        subprocess.Popen(['xdg-open', path])


class QueueWriter:
    """stdout/stderr replacement that feeds the log box from the worker thread."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(('log', s))

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = core.settings()
        self.q = queue.Queue()
        self.paks = []
        self.out_custom = False
        self.busy = False
        self.last = None
        root.title('Sandstorm Map Rebuilder v%s' % __version__)
        try:
            root.iconbitmap(core.resource('app', 'smr.ico'))
        except tk.TclError:
            pass  # not on Windows, or the icon is missing: keep the default
        root.minsize(760, 640)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self._build_extract()
        self._build_result()
        self._build_sdk()
        self._refresh_sdk()
        root.after(100, self._poll)

    # ------------------------------------------------------------------ layout
    def _build_extract(self):
        f = ttk.LabelFrame(self.root, text=' 1  Extract a map mod ', padding=PAD)
        f.grid(row=0, column=0, sticky='nsew', padx=PAD, pady=(PAD, 4))
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text="The mod's .pak files (all of them: chunk0, chunk1, optional, Sound...).",
                  foreground='#555').grid(row=0, column=0, columnspan=2, sticky='w')
        self.pak_list = tk.Listbox(f, height=4, activestyle='none')
        self.pak_list.grid(row=1, column=0, sticky='nsew', pady=4)
        btns = ttk.Frame(f)
        btns.grid(row=1, column=1, sticky='n', padx=(PAD, 0), pady=4)
        ttk.Button(btns, text='Add folder...', command=self.add_folder).pack(fill='x')
        ttk.Button(btns, text='Add .pak files...', command=self.add_files).pack(fill='x', pady=2)
        ttk.Button(btns, text='Clear', command=self.clear_paks).pack(fill='x')
        out = ttk.Frame(f)
        out.grid(row=2, column=0, columnspan=2, sticky='ew')
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text='Output folder:').grid(row=0, column=0, sticky='w')
        self.out_var = tk.StringVar()
        ttk.Entry(out, textvariable=self.out_var).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(out, text='Change...', command=self.pick_out).grid(row=0, column=2)
        ttk.Label(out, text='New mod name:').grid(row=1, column=0, sticky='w', pady=(4, 0))
        self.name_var = tk.StringVar()
        self.name_custom = False
        name = ttk.Entry(out, textvariable=self.name_var, width=28)
        name.grid(row=1, column=1, sticky='w', padx=4, pady=(4, 0))
        name.bind('<Key>', lambda e: setattr(self, 'name_custom', True))
        ttk.Label(out, text="the mod you'll create in the SDK. Keep it different from the original's, or players "
                            "with both installed load the original.", foreground='#555', wraplength=380,
                  justify='left').grid(row=1, column=2, sticky='w', pady=(4, 0))
        run = ttk.Frame(f)
        run.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(PAD, 0))
        run.columnconfigure(1, weight=1)
        self.extract_btn = ttk.Button(run, text='Extract', command=self.start_extract, state='disabled')
        self.extract_btn.grid(row=0, column=0)
        self.progress = ttk.Progressbar(run, mode='indeterminate')
        self.progress.grid(row=0, column=1, sticky='ew', padx=PAD)

    def _build_result(self):
        f = ttk.LabelFrame(self.root, text=' 2  Result ', padding=PAD)
        f.grid(row=1, column=0, sticky='nsew', padx=PAD, pady=4)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky='ew')
        top.columnconfigure(0, weight=1)
        self.summary = ttk.Label(top, text='Add a map\'s paks above, then Extract.', wraplength=560, justify='left')
        self.summary.grid(row=0, column=0, sticky='w')
        self.open_folder_btn = ttk.Button(top, text='Open folder', state='disabled',
                                          command=lambda: open_path(self.last['folder']))
        self.open_folder_btn.grid(row=0, column=1, padx=2)
        self.open_report_btn = ttk.Button(top, text='Open report', state='disabled',
                                          command=lambda: open_path(os.path.join(self.last['folder'], 'REPORT.md')))
        self.open_report_btn.grid(row=0, column=2)
        # the next step, set apart so it isn't lost in the summary
        self.next_step = tk.Label(top, text='', justify='left', anchor='w', wraplength=700,
                                  bg='#FFF4CC', fg='#000000', relief='solid', bd=1, padx=8, pady=6,
                                  font=('Segoe UI', 10, 'bold'))
        box = ttk.Frame(f)
        box.grid(row=1, column=0, sticky='nsew', pady=(4, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log = tk.Text(box, height=12, wrap='word', state='disabled', font=('Consolas', 9))
        self.log.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(box, command=self.log.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self.log['yscrollcommand'] = sb.set

    def _build_sdk(self):
        f = ttk.LabelFrame(self.root, text=' 3  Rebuild in the SDK ', padding=PAD)
        f.grid(row=2, column=0, sticky='nsew', padx=PAD, pady=(4, PAD))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text='SDK folder:').grid(row=0, column=0, sticky='w')
        self.sdk_var = tk.StringVar(value=self.cfg.get('sdk') or '')
        ttk.Entry(f, textvariable=self.sdk_var).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(f, text='Browse...', command=self.pick_sdk).grid(row=0, column=2)
        ttk.Button(f, text='Find', command=self.find_sdk).grid(row=0, column=3, padx=(2, 0))
        row = ttk.Frame(f)
        row.grid(row=1, column=0, columnspan=4, sticky='ew', pady=(PAD, 0))
        row.columnconfigure(0, weight=1)
        self.sdk_status = ttk.Label(row, text='', wraplength=520, justify='left')
        self.sdk_status.grid(row=0, column=0, sticky='w')
        self.install_btn = ttk.Button(row, text='Install SMR menu', command=self.install)
        self.install_btn.grid(row=0, column=1, padx=2)
        self.remove_btn = ttk.Button(row, text='Remove', command=self.uninstall)
        self.remove_btn.grid(row=0, column=2)
        self.sdk_var.trace_add('write', lambda *a: self._refresh_sdk())

    # ------------------------------------------------------------------ paks / output
    def _set_paks(self, paths):
        self.paks = core.find_paks(self.paks + paths)
        self.pak_list.delete(0, 'end')
        for p in self.paks:
            self.pak_list.insert('end', '%s      (%s)' % (os.path.basename(p), os.path.dirname(p)))
        if self.paks and not self.out_custom:
            self.out_var.set(core.default_output(self.paks))
        if self.paks and not self.name_custom:
            self.name_var.set(core.default_target(core.original_mod(self.paks)))
        self.extract_btn['state'] = 'normal' if self.paks and not self.busy else 'disabled'

    def add_folder(self):
        start = self.cfg.get('last_pak_dir') or (MODIO_DIR if os.path.isdir(MODIO_DIR) else '')
        d = filedialog.askdirectory(title="Folder with the mod's .pak files", initialdir=start or None)
        if d:
            self.cfg['last_pak_dir'] = d
            before = len(self.paks)
            self._set_paks([d])
            if len(self.paks) == before:
                messagebox.showinfo('No paks', 'No .pak files in %s (or its subfolders).' % d)

    def add_files(self):
        start = self.cfg.get('last_pak_dir') or (MODIO_DIR if os.path.isdir(MODIO_DIR) else '')
        fs = filedialog.askopenfilenames(title='Map .pak files', initialdir=start or None,
                                         filetypes=[('Unreal pak', '*.pak'), ('All files', '*.*')])
        if fs:
            self.cfg['last_pak_dir'] = os.path.dirname(fs[0])
            self._set_paks(list(fs))

    def clear_paks(self):
        self.paks = []
        self.out_custom = False
        self.out_var.set('')
        self.name_custom = False
        self.name_var.set('')
        self._set_paks([])

    def pick_out(self):
        d = filedialog.askdirectory(title='Output folder for the extracted map')
        if d:
            self.out_custom = True
            self.out_var.set(d)

    # ------------------------------------------------------------------ extraction
    def start_extract(self):
        out = self.out_var.get().strip()
        if not self.paks or not out:
            return
        target = core.clean_mod_name(self.name_var.get().strip())
        if self.name_var.get().strip() and not target:
            messagebox.showerror('Mod name', 'A mod name can only use letters, digits and underscores, and must '
                                             'start with a letter.')
            return
        if os.path.exists(os.path.join(out, 'layout.json')) and not messagebox.askyesno(
                'Overwrite?', '%s already has an extracted map.\nExtract again and overwrite it?' % out):
            return
        self.busy = True
        self.extract_btn['state'] = 'disabled'
        self.progress.start(12)
        self._clear_log()
        self.next_step.grid_remove()
        self.summary['text'] = 'Extracting %s... (a big map takes a few minutes)' % core.map_name(self.paks)
        core.save_settings(self.cfg)
        threading.Thread(target=self._worker, args=(list(self.paks), out, target or None), daemon=True).start()

    def _worker(self, paks, out, target=None):
        w = QueueWriter(self.q)
        old = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = w
        try:
            print('Sandstorm Map Rebuilder v%s %s\n' % (__version__, __build__))
            self.q.put(('done', core.extract(paks, out, target)))
        except Exception as ex:
            traceback.print_exc()
            self.q.put(('error', ex))
        finally:
            sys.stdout, sys.stderr = old

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'log':
                    self._append(val)
                elif kind == 'done':
                    self._finished(val)
                elif kind == 'error':
                    self._failed(val)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finished(self, s):
        self.busy = False
        self.last = s
        self.progress.stop()
        self.extract_btn['state'] = 'normal'
        self.open_folder_btn['state'] = self.open_report_btn['state'] = 'normal'
        bits = ['%d levels' % s['levels'], '%d actors' % s['actors'], '%d custom meshes' % s['meshes'],
                '%d textures' % s['textures']]
        if s['stand_ins']:
            bits.append('%d custom materials (stand-ins)' % s['stand_ins'])
        if s['landscapes']:
            bits.append('%d landscape(s) to import by hand' % s['landscapes'])
        if s['mesh_errors']:
            bits.append('%d meshes FAILED' % s['mesh_errors'])
        self.summary['text'] = '%s extracted in %ds: %s.' % (s['map'], s['seconds'], ', '.join(bits))
        same = s['target'] == s['mod']
        pf = s.get('preflight') or {'tier': 'easy', 'checks': []}
        warns = [c['msg'] for c in pf['checks'] if c['level'] == 'warn']
        head = 'DIFFICULTY: %s\n' % preflight.TIERS[pf['tier']]
        if warns:
            head += '\nCHECK BEFORE REBUILDING (also at the top of the report):\n' + ''.join(
                '- %s\n' % w for w in warns)
        self.next_step['text'] = head + ('\nNEXT, in the SDK:\n'
                                  '1. Create a mod named "%s" (exactly that name).%s\n'
                                  '2. Run SMR > %s > Rebuild map.\n'
                                  '3. Work through the TODO list, which opens in your browser when the rebuild finishes.' % (
                                      s['target'],
                                      # the same-name warning is already in CHECK BEFORE REBUILDING above
                                      '' if same else
                                      ' The map becomes %s, so it never clashes with the original.' % (
                                          s.get('new_map') or s['target']),
                                      s['map']))
        self.next_step.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(6, 0))
        try:
            with open(os.path.join(s['folder'], 'REPORT.md'), encoding='utf-8') as fh:
                report = fh.read()
            self._append('\n' + '=' * 60 + '\nREPORT.md\n' + '=' * 60 + '\n' + report)
        except (OSError, ValueError):
            pass
        self._refresh_sdk()

    def _failed(self, ex):
        self.busy = False
        self.progress.stop()
        self.extract_btn['state'] = 'normal'
        self.summary['text'] = 'Extraction failed: %s\nThe log below has the details.' % ex

    def _append(self, s):
        self.log['state'] = 'normal'
        self.log.insert('end', s)
        self.log.see('end')
        self.log['state'] = 'disabled'

    def _clear_log(self):
        self.log['state'] = 'normal'
        self.log.delete('1.0', 'end')
        self.log['state'] = 'disabled'

    # ------------------------------------------------------------------ SDK
    def pick_sdk(self):
        d = filedialog.askdirectory(title='Sandstorm SDK folder (the one containing the "Insurgency" folder)')
        if d:
            self.sdk_var.set(os.path.normpath(d))

    def find_sdk(self):
        d = core.find_sdk()
        if d:
            self.sdk_var.set(d)
        else:
            messagebox.showinfo('SDK not found', 'Could not find the SDK in the usual Steam / Epic folders.\n'
                                                 'Use Browse and pick the folder that contains "Insurgency".')

    def _refresh_sdk(self):
        sdk = self.sdk_var.get().strip()
        if not sdk:
            self.sdk_status['text'] = 'Pick the SDK folder (or click Find) to install the SMR editor menu.'
            self.install_btn['state'] = self.remove_btn['state'] = 'disabled'
            return
        if not core.is_sdk(sdk):
            self.sdk_status['text'] = 'Not the SDK folder: it should contain Insurgency\\Insurgency.uproject.'
            self.install_btn['state'] = self.remove_btn['state'] = 'disabled'
            return
        self.cfg['sdk'] = sdk
        core.save_settings(self.cfg)
        v = core.menu_installed(sdk)
        if v is None:
            self.sdk_status['text'] = 'SMR menu not installed. Install it, then restart the SDK once.'
            self.install_btn['text'], self.install_btn['state'], self.remove_btn['state'] = \
                'Install SMR menu', 'normal', 'disabled'
        elif v != __version__ or core.installed_build(sdk) != __build__:
            self.sdk_status['text'] = 'SMR menu %s installed; this app is v%s %s. Update it, then restart the SDK.' % (
                ('v%s %s' % (v, core.installed_build(sdk) or '')).strip(), __version__, __build__)
            self.install_btn['text'], self.install_btn['state'], self.remove_btn['state'] = \
                'Update SMR menu', 'normal', 'normal'
        else:
            n = len(core.extracted_maps())
            self.sdk_status['text'] = ('SMR menu v%s installed. In the editor: SMR > <map> > Rebuild map '
                                       '(%d extracted map%s listed; SMR > Refresh map list picks up new ones).' % (
                                           v, n, '' if n == 1 else 's'))
            self.install_btn['text'], self.install_btn['state'], self.remove_btn['state'] = \
                'Reinstall', 'normal', 'normal'

    def install(self):
        try:
            core.install_menu(self.sdk_var.get().strip())
        except Exception as ex:
            messagebox.showerror('Install failed', str(ex))
            return
        self._refresh_sdk()
        messagebox.showinfo('SMR menu installed', 'Restart the SDK once. The SMR menu then appears in the menu bar '
                                                  'and lists every map you extract.')

    def uninstall(self):
        core.uninstall_menu(self.sdk_var.get().strip())
        self._refresh_sdk()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista' if sys.platform == 'win32' else 'clam')
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
