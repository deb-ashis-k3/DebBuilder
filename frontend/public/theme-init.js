// Applies the saved theme before the page paints. Default: none, i.e. the Slate theme following the
// system's light / dark setting. A separate file, not inline: the Content-Security-Policy only allows
// scripts served by DebBuilder.
try {
  var t = localStorage.getItem('debbuilder-ui-theme')
  if (t) document.documentElement.dataset.theme = t
} catch (e) { /* storage blocked: keep the default */ }
