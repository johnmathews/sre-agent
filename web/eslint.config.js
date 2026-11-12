// ESLint flat config for the web UI (#56). Error-prevention rules only: Vue's `essential` set and
// typescript-eslint's `recommended` set, adapted for .vue files by @vue/eslint-config-typescript.
// Vue's formatting rules (`strongly-recommended` and up) are left off: the project has no
// formatter, and lint should catch bugs, not restyle templates.
import pluginVue from 'eslint-plugin-vue'
import { withVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import globals from 'globals'

export default withVueTs(
  { ignores: ['dist/**', 'playwright-report/**', 'test-results/**', 'node_modules/**'] },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  {
    rules: {
      // Model output is rendered as HTML. Every v-html must go through src/lib/markdown.ts
      // (marked -> DOMPurify) and carry a disable comment saying so.
      'vue/no-v-html': 'error',
      // Sidebar predates the linter and cannot clash with an HTML element; renaming it buys nothing.
      'vue/multi-word-component-names': ['error', { ignores: ['Sidebar'] }],
    },
  },
  {
    files: ['scripts/**/*.mjs'],
    languageOptions: { globals: globals.node },
  },
)
