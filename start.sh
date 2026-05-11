#!/bin/bash
cd /Users/mozengfu/workspace/quant-system
export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin"
exec /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 app.py >> logs/app.log 2>&1
