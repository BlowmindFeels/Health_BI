@echo off
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name HealthBI --paths app --collect-submodules odf --collect-submodules openpyxl --hidden-import sqlalchemy.dialects.sqlite --hidden-import PIL._tkinter_finder app\main.py
echo.
echo Listo: dist\HealthBI.exe
pause