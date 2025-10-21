@echo off

cls
echo [INFO] Resolvendo dependencias...
python -m pip install -U -q pip
pip install -q -r requirements.txt
python page_pfp.py
pause