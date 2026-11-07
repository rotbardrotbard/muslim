# -*- coding: utf-8 -*-
"""
Сервер напоминаний «Умма в ОК» (пример для production).

Схема работы:
  1. Мини-приложение сохраняет предпочтения пользователя:
     POST /api/subscribe  {uid, city, lang, minutes_before, access_token}
     access_token пользователь выдаёт при первом запуске через OAuth ОК
     (https://connect.ok.ru/oauth/authorize, scope: VALUABLE_ACCESS).
  2. Cron (раз в минуту): GET /api/cron  — сервер для каждого пользователя
     вычисляет следующий намаз и, если сейчас = «время намаза − minutes_before»,
     отправляет уведомление через OK REST API:
     method=notifications.sendSimple  (apiok.ru/dev/methods/rest/notifications)

Важно: этот файл — рабочий шаблон. Перед запуском в продакшн:
  - подставьте OK_APPLICATION_KEY и OK_APPLICATION_SECRET_KEY;
  - вынесите OK_ACCESS_TOKEN каждого пользователя в БД;
  - замените демо-таблицу времён намаза на расчёт по координатам города
    (например, библиотекой adhan-python) или внешним API;
  - уточните лимиты и права для notifications.sendSimple в поддержке ОК
    (api-support@ok.ru) — метод доступен приложениям всех типов, кроме игр.

Запуск: pip install flask && python push_server.py   (порт 5000)
Cron:   * * * * * curl -s http://127.0.0.1:5000/api/cron
"""
import hashlib
import json
import os
import time

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------- КОНФИГУРАЦИЯ (подставить свои значения) ----------------
OK_APP_KEY = os.environ.get('OK_APP_KEY', 'CBAXXXXBABABABA')      # application_key
OK_APP_SECRET = os.environ.get('OK_APP_SECRET', 'XXXXXXXXXXXXXXXX')  # application_secret_key
OK_API = 'https://api.ok.ru/fb.do'
USERS_FILE = os.path.join(os.path.dirname(__file__), 'users.json')

# Демо-времена намаза (Москва). В продакшн — расчёт по координатам города
# (библиотека adhan-python / adhan-js) или внешний API времени намаза.
PRAYER_TIMES = {'f': '06:12', 'z': '12:03', 'a': '14:23', 'm': '16:44', 'i': '18:06'}
PRAYER_NAMES = {
    'ru': {'f': 'Фаджр', 'z': 'Зухр', 'a': 'Аср', 'm': 'Магриб', 'i': 'Иша'},
    'kk': {'f': 'Таң', 'z': 'Бесін', 'a': 'Екінті', 'm': 'Ақшам', 'i': 'Құптан'},
    'uz': {'f': 'Bomdod', 'z': 'Peshin', 'a': 'Asr', 'm': 'Shom', 'i': 'Xufton'},
    # остальные языки добавляются аналогично (kk/uz/ky/tg/tt/ba/ce/av)
}

# ---------------- ХРАНИЛИЩЕ (для примера — JSON-файл) ----------------
def _users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, encoding='utf-8') as f:
        return json.load(f)

def _save_users(u):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(u, f, ensure_ascii=False, indent=1)

# ---------------- ПОДПИСЬ ЗАПРОСА ОК ----------------
def ok_sig(params: dict, secret_key: str) -> str:
    """sig = md5( отсортированные k=v подряд + secret_key ), hex, строчные."""
    s = ''.join(f'{k}={params[k]}' for k in sorted(params)) + secret_key
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def send_ok_notification(uid: str, text: str, access_token: str):
    """notifications.sendSimple — простое оповещение от приложения пользователю."""
    params = {
        'application_key': OK_APP_KEY,
        'format': 'json',
        'method': 'notifications.sendSimple',
        'text': text,
        'type': 'info',
        'uids': uid,
    }
    params['sig'] = ok_sig(params, OK_APP_SECRET)
    params['access_token'] = access_token
    r = requests.post(OK_API, data=params, timeout=10)
    return r.json()

# ---------------- HTTP API ----------------
@app.post('/api/subscribe')
def subscribe():
    """Мини-приложение сохраняет настройки напоминаний пользователя."""
    d = request.get_json(force=True)
    uid = str(d.get('uid', '')).strip()
    if not uid:
        return jsonify(ok=False, error='uid required'), 400
    users = _users()
    users[uid] = {
        'city': d.get('city', 'Москва'),
        'lang': d.get('lang', 'ru'),
        'minutes_before': int(d.get('minutes_before', 10)),
        'access_token': d.get('access_token', ''),
        'updated': int(time.time()),
    }
    _save_users(users)
    return jsonify(ok=True)

@app.post('/api/unsubscribe')
def unsubscribe():
    d = request.get_json(force=True)
    uid = str(d.get('uid', ''))
    users = _users()
    users.pop(uid, None)
    _save_users(users)
    return jsonify(ok=True)

@app.get('/api/cron')
def cron():
    """Вызывается планировщиком раз в минуту. Рассылает напоминания.

    Демо-логика: проверяем «Фаджр − minutes_before». В продакшн сервер
    считает все пять намазов по координатам города пользователя.
    """
    now = time.strftime('%H:%M')
    sent, skipped = [], 0
    for uid, u in _users().items():
        mb = u.get('minutes_before', 10)
        h, m = map(int, PRAYER_TIMES['f'].split(':'))
        total = h * 60 + m - mb
        target = f'{(total // 60) % 24:02d}:{total % 60:02d}'
        if now != target:
            skipped += 1
            continue
        lang = u.get('lang', 'ru')
        names = PRAYER_NAMES.get(lang, PRAYER_NAMES['ru'])
        text = f"🕌 Умма в ОК\nЧерез {mb} минут — {names['f']} ({PRAYER_TIMES['f']})"
        if u.get('access_token'):
            res = send_ok_notification(uid, text, u['access_token'])
            sent.append({'uid': uid, 'result': res})
        else:
            sent.append({'uid': uid, 'result': 'нет access_token (нужен OAuth)'})
    return jsonify(ok=True, now=now, sent=sent, skipped=skipped)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
