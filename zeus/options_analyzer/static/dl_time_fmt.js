// Single source of truth for timestamp formatting across the Decision Log.
// Browser usage: <script src="/static/dl_time_fmt.js"> exposes window._dlFmt
// Node test usage: require('./static/dl_time_fmt.js') exposes module.exports
//
// Every function here uses Date.toLocaleString WITHOUT a timeZone option, so
// the user's machine timezone is the source of truth. If the user travels to
// another timezone, the same ISO timestamp will render in that new local
// time on next page load.

(function (root) {
  'use strict';

  function _toDate(iso) {
    if (!iso) return null;
    if (iso instanceof Date) return iso;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  // Compact "Apr 29, 14:37" — used for chart axis ticks and short labels.
  function fmtShort(iso) {
    var d = _toDate(iso);
    if (!d) return iso || '';
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: false,
    });
  }

  // "04-29 14:37" — fixed-width sortable, good for tables.
  function fmtTableTs(iso) {
    var d = _toDate(iso);
    if (!d) return iso || '';
    var pad = function (n) { return String(n).padStart(2, '0'); };
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
           pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  // "Apr 29, 11:17 AM" — used for drawer header where 12-hour is friendlier.
  function fmtFriendly(iso) {
    var d = _toDate(iso);
    if (!d) return iso || '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
           ' ' +
           d.toLocaleTimeString(undefined, {
              hour: 'numeric', minute: '2-digit', hour12: true,
            });
  }

  // "Apr 29 23:42" — for verbose status lines showing first/last snapshot.
  function fmtVerbose(iso) {
    var d = _toDate(iso);
    if (!d) return iso || '';
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
  }

  // Returns the resolved IANA timezone the browser is using (best-effort).
  function localTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch (e) {
      return '';
    }
  }

  var api = { fmtShort: fmtShort, fmtTableTs: fmtTableTs,
              fmtFriendly: fmtFriendly, fmtVerbose: fmtVerbose,
              localTimezone: localTimezone };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (root) root._dlFmt = api;
})(typeof window !== 'undefined' ? window : null);
