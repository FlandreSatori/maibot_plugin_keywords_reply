@echo off
REM 本机访问：http://127.0.0.1:8765
python .\editor\server.py --data-dir ..\..\data\plugins\maibot_plugin.keywords_reply --host 127.0.0.1 --port 8765
