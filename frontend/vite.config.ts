import react from '@vitejs/plugin-react'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, type Plugin } from 'vite'

// Override to point the editor at another API instance: BACKEND_URL=http://localhost:8001 npm run dev
const backend = process.env.BACKEND_URL ?? 'http://localhost:8000'
const here = dirname(fileURLToPath(import.meta.url))

function files(dir: string, exts: string[], out: string[] = []): string[] {
  let entries: string[] = []
  try { entries = readdirSync(dir) } catch { return out }
  for (const name of entries) {
    if (name === 'node_modules' || name === '__pycache__' || name.startsWith('.')) continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) files(p, exts, out)
    else if (exts.some((e) => name.endsWith(e))) out.push(p)
  }
  return out
}

/**
 * `virtual:used-icons`: only the lucide icons the app can show. Importing lucide's `icons` object bundles all
 * ~1,600; instead every quoted kebab-case string in the editor and in the backend's node / credential
 * definitions (where node icons come from) that names a lucide icon is imported by name, so the rest is
 * tree-shaken. A superset is fine (a common word that is also an icon just adds one tiny icon).
 * Dev serves the full set so new icons work without a restart.
 */
function usedIcons(): Plugin {
  const id = 'virtual:used-icons'
  let build = false
  return {
    name: 'used-icons',
    configResolved(c) { build = c.command === 'build' },
    resolveId(source) { return source === id ? '\0' + id : null },
    load(resolved) {
      if (resolved !== '\0' + id) return null
      if (!build) return `import { icons } from 'lucide-react'\nexport default icons\n`
      const lucide = readFileSync(resolve(here, 'node_modules/lucide-react/dist/esm/lucide-react.mjs'), 'utf8')
      const exported = new Set([...lucide.matchAll(/default as (\w+)/g)].map((m) => m[1]))
      const toPascal = (s: string) => s.replace(/(^|-)([a-z0-9])/g, (_, __, c: string) => c.toUpperCase())
      const sources = [
        ...files(resolve(here, 'src'), ['.ts', '.tsx']),
        ...files(resolve(here, '../backend/app'), ['.py']),
      ]
      const names = new Set<string>(['Box'])
      for (const f of sources) {
        for (const m of readFileSync(f, 'utf8').matchAll(/["'`]([a-z][a-z0-9]*(?:-[a-z0-9]+)*)["'`]/g)) {
          const p = toPascal(m[1])
          if (exported.has(p)) names.add(p)
        }
      }
      const list = [...names].sort()
      this.info?.(`${list.length} icons bundled`)
      return `import { ${list.join(', ')} } from 'lucide-react'\nexport default { ${list.join(', ')} }\n`
    },
  }
}

/**
 * Preload the Latin Inter font from index.html. Otherwise the browser finds it only after downloading and
 * parsing the CSS (HTML → CSS → font), which delays the first text paint.
 */
function preloadFont(): Plugin {
  return {
    name: 'preload-font',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        const font = Object.keys(ctx.bundle ?? {}).find((f) => /inter-latin-wght-normal-[\w-]+\.woff2$/.test(f))
        if (!font) return html
        return html.replace('</title>', `</title>\n    <link rel="preload" href="/${font}" as="font" type="font/woff2" crossorigin>`)
      },
    },
  }
}

export default defineConfig({
  plugins: [react(), usedIcons(), preloadFont()],
  build: {
    rollupOptions: {
      output: {
        // Libraries change less often than the app: separate files stay cached across releases.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('livekit')) return 'voice'
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/.test(id)) return 'react'
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/webhook': backend,
      '/webhook-test': backend,
      '/form': backend,
      '/ws': { target: backend, ws: true },
    },
  },
})
