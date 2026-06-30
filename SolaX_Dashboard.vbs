' Spusti SolaX dashboard uplne skryte (bez okna konzole).
' Uprav cestu, pokud mas projekt jinde nez C:\Solax
' Pokud neni pythonw v PATH, dej plnou cestu, napr.:
'   "C:\Users\<jmeno>\AppData\Local\Programs\Python\Python313\pythonw.exe"

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Solax"
sh.Run """C:\Users\p-zah\AppData\Local\Programs\Python\Launcher\pyw.exe"" solax_desktop.py", 0, False
