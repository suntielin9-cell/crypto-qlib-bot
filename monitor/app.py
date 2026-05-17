"""Simple monitoring dashboard for auto_trader.py + Qlib."""
import json, os, sys
from pathlib import Path
from flask import Flask, jsonify, render_template

app = Flask(__name__)

BASE = Path(__file__).resolve().parent.parent
STATS_FILE = BASE / 'qlib_stats.json'


def load_stats():
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/stats')
def api_stats():
    return jsonify(load_stats())


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5050, debug=False)
