#!/usr/bin/env python3
"""No hardware required; checks mock mode logs exactly 3 records."""
import os
import subprocess
import sys
import tempfile
import json
import sqlite3

root = os.path.dirname(os.path.abspath(__file__))
with tempfile.TemporaryDirectory() as d:
    cfg = json.load(open(os.path.join(root, 'config.example.json'), encoding='utf-8'))
    cfg['database'] = os.path.join(d, 'test.sqlite3')
    conf_path = os.path.join(d, 'config.json')
    with open(conf_path, 'w', encoding='utf-8') as out:
        json.dump(cfg, out)
    result = subprocess.run([sys.executable, os.path.join(root, 'main.py'),
       '--mode', 'mock', '--config', conf_path, '--samples', '3'], check=True,
       capture_output=True, text=True)
    con = sqlite3.connect(cfg['database'])
    n = con.execute("SELECT count(*) FROM sensor_raw WHERE status='OK' AND mode='mock'").fetchone()[0]
    assert n == 3, n
    con.close()
    print('PASS: 3 mock samples saved to SQLite; NO actual device access')
