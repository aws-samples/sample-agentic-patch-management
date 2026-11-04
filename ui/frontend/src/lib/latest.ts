/**
 * For requests that can overlap: begin() returns a check that is true only while
 * no later request has begun, so an older response arriving late is dropped.
 */
export function latest() {
  let n = 0;
  return {
    begin(): () => boolean {
      const mine = ++n;
      return () => mine === n;
    },
  };
}
