/* Tailwind (Play CDN) theme for the paperbot dashboard. Loaded right after the CDN script. */
if (window.tailwind) {
  window.tailwind.config = {
    theme: {
      extend: {
        colors: {
          ink: {
            950: '#05080d', 900: '#0a0f17', 850: '#0d131d', 800: '#111926',
            700: '#1a2433', 600: '#253144', 500: '#3a475b', 400: '#5d6b80', 300: '#8b98aa',
          },
        },
        fontFamily: {
          sans: ['Inter', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'sans-serif'],
          mono: ['JetBrains Mono', 'ui-monospace', 'Cascadia Mono', 'Consolas', 'monospace'],
        },
      },
    },
  };
}
