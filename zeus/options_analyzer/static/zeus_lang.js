/* ZEUS Terminal: the interface language, known before any page script runs (loaded in <head>).
 *
 * zeus.js translates Chinese interface text with the dictionary in i18n_en.json, one whole
 * sentence at a time. A sentence a page builds with numbers in it cannot be a dictionary key, so
 * the page gives its English at the source instead:
 *
 *   zt('剩 ' + n + ' 天', n + ' days left')     English mode → English, otherwise the Chinese
 *   ztt(zh, en)                                 for a tooltip: bilingual mode shows both
 *
 * zeus.js leaves the English alone (it has no Chinese left to translate). */
(function () {
  'use strict';
  if (window.zt) return;
  var lang = null;
  try { lang = localStorage.getItem('zeusLang'); } catch (e) {}
  if (lang !== 'zh' && lang !== 'bi' && lang !== 'en')
    lang = (navigator.language || '').toLowerCase().indexOf('zh') === 0 ? 'zh' : 'en';
  // zeus.js replaces this with its own copy, which picks the same language the same way
  if (!window.zeusLang) window.zeusLang = function () { return lang; };
  window.zt = function (zh, en) { return window.zeusLang() === 'en' ? en : zh; };
  window.ztt = function (zh, en) {
    var l = window.zeusLang();
    return l === 'en' ? en : (l === 'bi' ? zh + '\n— ' + en : zh);
  };
})();
