@echo off
REM 可选：在末尾加 --token 你的密码，然后用 http://127.0.0.1:8765/?token=你的密码 访问
python .\editor\server.py --data-dir ..\..\data\plugins\maibot_plugin.keywords_reply --port 8765 --token 123
