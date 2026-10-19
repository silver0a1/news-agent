# Vendored: Mozilla Readability

Unmodified copies of `Readability.js`, `Readability-readerable.js` and `LICENSE.md`
from the npm package `@mozilla/readability` **0.6.0** (Apache-2.0),
https://github.com/mozilla/readability — the engine behind Firefox Reader View.

`news_agent/browser.py` inlines both files into a Playwright `page.evaluate` call.

To update:

```bash
npm pack @mozilla/readability@<version>
tar xzf mozilla-readability-<version>.tgz
cp package/Readability.js package/Readability-readerable.js package/LICENSE.md news_agent/vendor/readability/
```

then bump the version above and run the tests.
