' Lanceur sans fenêtre console pour Plume (dictée vocale).
' Double-cliquez ce fichier pour démarrer « Plume » proprement.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' pythonw.exe = interpréteur Python sans console ; 0 = fenêtre du lanceur masquée.
sh.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\plume.py""", 0, False
