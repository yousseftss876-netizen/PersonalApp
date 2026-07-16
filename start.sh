#!/bin/bash
/home/runner/workspace/.pythonlibs/bin/gunicorn --bind 0.0.0.0:5000 --reuse-port --reload main:app
