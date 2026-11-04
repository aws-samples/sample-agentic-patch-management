/**
 * Server-sent events from a fetch body, one parsed JSON object per event.
 *
 * Handles events split across reads, multi-line `data:` fields, `data:`
 * with or without a space, comments (keepalives), and a final event with no
 * trailing blank line.
 */
export class SseParser {
  private buffer = "";
  private data: string[] = [];

  /** Feed decoded text; returns the events it completed. */
  push(text: string): unknown[] {
    this.buffer += text;
    const events: unknown[] = [];
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newline).replace(/\r$/, "");
      this.buffer = this.buffer.slice(newline + 1);
      const event = this.line(line);
      if (event !== undefined) events.push(event);
    }
    return events;
  }

  /** End of stream: whatever is buffered counts as a complete event. */
  end(): unknown[] {
    const events: unknown[] = [];
    if (this.buffer) {
      const event = this.line(this.buffer.replace(/\r$/, ""));
      this.buffer = "";
      if (event !== undefined) events.push(event);
    }
    const last = this.dispatch();
    if (last !== undefined) events.push(last);
    return events;
  }

  private line(line: string): unknown {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) return undefined; // comment, such as a keepalive
    if (line.startsWith("data:")) this.data.push(line.slice(5).replace(/^ /, ""));
    return undefined;
  }

  private dispatch(): unknown {
    if (!this.data.length) return undefined;
    const raw = this.data.join("\n");
    this.data = [];
    try {
      return JSON.parse(raw);
    } catch {
      return undefined; // a malformed event is skipped, not fatal
    }
  }
}
