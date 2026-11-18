const test = require('node:test');
const assert = require('node:assert/strict');
const {split} = require('./split.cjs');
for (const [input, expected] of [
  ['"a;b";c', ['"a;b"', 'c']],
  ["'a;b';c", ["'a;b'", 'c']],
  [';x;', ['', 'x', '']],
]) {
  test('quoted delimiter: ' + JSON.stringify(input), () => {
    assert.deepEqual(split(input), expected);
  });
}
