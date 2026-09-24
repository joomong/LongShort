import { cp, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const monacoRoot = dirname(require.resolve('monaco-editor/package.json'));
const destination = join(frontendRoot, 'public', 'monaco', 'vs');

// Serve the editor and its workers from this app, including on offline networks.
await mkdir(destination, { recursive: true });
await cp(join(monacoRoot, 'min', 'vs'), destination, { recursive: true });
console.log('Prepared local Monaco editor assets.');
