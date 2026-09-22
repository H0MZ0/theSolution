#!/usr/bin/env node
'use strict';
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const { writeSync } = require('node:fs');
const result = spawnSync(process.env.GOINFRE_PYTHON || 'python3', [
  path.resolve(__dirname, '../bootstrap.py'), ...process.argv.slice(2),
], { stdio: 'inherit', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
if (result.error) writeSync(2, `theSolution: Python 3.11+ with venv is required: ${result.error.message}\n`);
process.exit(result.status ?? (result.signal === 'SIGINT' ? 130 : 1));
