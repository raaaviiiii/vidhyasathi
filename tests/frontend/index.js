// Entry point so `node --test tests/frontend/` works on Node 22 (a directory argument is
// resolved as a module, i.e. this index.js). Loads every *.test.mjs in this folder.
// Equivalent explicit form: node --test 'tests/frontend/*.test.mjs'
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const files = fs.readdirSync(__dirname).filter(f => f.endsWith('.test.mjs')).sort();
  for (const f of files) await import(pathToFileURL(path.join(__dirname, f)).href);
})();
