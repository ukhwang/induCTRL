#!/usr/bin/env python3
"""InduCTRL phase-1: MOCK or READ-ONLY Modbus TCP logger.

No motor-control writes are present in this program.
Requires Python 3.8+; live mode needs pymodbus==3.6.9.
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone


def load_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg


def validate_live_config(cfg):
    required = ('host', 'port', 'unit_id', 'function_code', 'address_zero_based', 'register_count')
    for name in required:
        if name not in cfg:
            raise ValueError('Missing live configuration: ' + name)
    if cfg.get('mapping_verified') is not True:
        raise ValueError('Live reads blocked: confirm iRTU Studio mapping and set mapping_verified=true')
    if int(cfg['function_code']) not in (3, 4):
        raise ValueError('function_code must be 3 (holding) or 4 (input)')
    address = cfg['address_zero_based']
    if not isinstance(address, int) or not 0 <= address <= 65535:
        raise ValueError('address_zero_based must be confirmed integer in range 0..65535')
    count = int(cfg['register_count'])
    if not 1 <= count <= 16 or address + count > 65536:
        raise ValueError('Invalid register_count or address range')
    if not 1 <= int(cfg['port']) <= 65535:
        raise ValueError('Invalid TCP port')
    if not 0 <= int(cfg['unit_id']) <= 247:
        raise ValueError('Invalid Modbus unit_id')


def init_database(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent)
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE IF NOT EXISTS sensor_raw (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        collected_at_utc TEXT NOT NULL,
        elapsed_seconds REAL NOT NULL,
        mode TEXT NOT NULL,
        source TEXT NOT NULL,
        function_code INTEGER,
        address_zero_based INTEGER,
        register_count INTEGER,
        registers_json TEXT,
        status TEXT NOT NULL,
        error_detail TEXT
    )''')
    con.commit()
    return con


def mock_read(index):
    # Demonstration bytes only, NOT turbidity/EC/RPM or an expected sensor signal.
    return [int(1000 + 60 * math.sin(index / 4.0)), 2000 + (index % 7)]


def run(args):
    cfg = load_config(args.config)
    interval = float(cfg.get('interval_seconds', 1.0))
    if not 0.2 <= interval <= 60.0:
        raise ValueError('interval_seconds must be 0.2..60')
    if args.mode == 'live':
        validate_live_config(cfg)
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError as exc:
            raise RuntimeError('Install pymodbus==3.6.9 for this Python 3.8-compatible example') from exc
        client = ModbusTcpClient(cfg['host'], port=int(cfg['port']), timeout=2)
        if not client.connect():
            raise ConnectionError('Could not connect to ADAM Modbus TCP endpoint')
    else:
        client = None

    con = init_database(cfg.get('database', 'data/inductrl_readings.sqlite3'))
    t0 = time.monotonic()
    seq = 0
    try:
        while args.samples == 0 or seq < args.samples:
            cycle_start = time.monotonic()
            now = datetime.now(timezone.utc).isoformat()
            values = None
            error = None
            try:
                if args.mode == 'mock':
                    values = mock_read(seq)
                else:
                    if int(cfg['function_code']) == 4:
                        response = client.read_input_registers(
                            address=cfg['address_zero_based'],
                            count=int(cfg['register_count']), slave=int(cfg['unit_id']))
                    else:
                        response = client.read_holding_registers(
                            address=cfg['address_zero_based'],
                            count=int(cfg['register_count']), slave=int(cfg['unit_id']))
                    if response.isError():
                        raise RuntimeError('Modbus exception response: ' + str(response))
                    values = list(response.registers)
            except Exception as exc:
                error = '{}: {}'.format(type(exc).__name__, exc)

            status = 'OK' if error is None else 'ERROR'
            con.execute('''INSERT INTO sensor_raw (
                batch_id,collected_at_utc,elapsed_seconds,mode,source,function_code,
                address_zero_based,register_count,registers_json,status,error_detail
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (
                args.batch_id, now, round(time.monotonic()-t0, 4), args.mode,
                cfg.get('host', 'mock'), int(cfg.get('function_code', 4)),
                cfg.get('address_zero_based'), int(cfg.get('register_count', 2)),
                json.dumps(values) if values is not None else None, status, error))
            con.commit()
            print('{} seq={} status={} raw={} {}'.format(now, seq, status, values, error or ''), flush=True)
            seq += 1
            remaining = interval - (time.monotonic() - cycle_start)
            if remaining > 0 and (args.samples == 0 or seq < args.samples):
                time.sleep(remaining)
    finally:
        con.close()
        if client:
            client.close()


def main():
    parser = argparse.ArgumentParser(description='InduCTRL read-only Modbus logger')
    parser.add_argument('--mode', choices=('mock', 'live'), default='mock')
    parser.add_argument('--config', default='config.example.json')
    parser.add_argument('--batch-id', default='setup-test')
    parser.add_argument('--samples', type=int, default=10,
                        help='0 = until Ctrl+C, positive integer = fixed number of reads')
    args = parser.parse_args()
    if args.samples < 0:
        parser.error('--samples must be >= 0')
    try:
        run(args)
    except KeyboardInterrupt:
        print('\nStopped by user')
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
