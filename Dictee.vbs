' Lanceur sans fenêtre console pour l'application de dictée.
' Double-cliquez ce fichier pour démarrer « Dictée » proprement.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' pythonw.exe = interpréteur Python sans console ; 0 = fenêtre du lanceur masquée.
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\dictee.py""", 0, False
