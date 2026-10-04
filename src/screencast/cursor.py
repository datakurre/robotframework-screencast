"""The injected cursor/click overlay, and human-paced input constants.

Playwright's native video has no mouse cursor and no click indicator -- see
the `browser` agent skill. CURSOR_SCRIPT is injected into every recorded
context with `context.add_init_script()` so a viewer can see what is doing
the clicking. It waits for DOMContentLoaded before touching
`document.documentElement` (an init script can run before the document
element exists), and re-creates itself at the page's centered default
position on every new document -- callers must move the pointer again after
a navigation, which `human_click()`/`human_move()` already do by moving to
their target before acting.
"""

CURSOR_SCRIPT = """
(() => {
  if (window.top !== window) return;
  const install = () => {
    const style = document.createElement('style');
    style.textContent = `
      #screencast-recording-cursor {
        position: fixed; left: 50%; top: 50%; z-index: 2147483647;
        width: 24px; height: 24px;
        border: 2px solid #ff3b30; border-radius: 50%; pointer-events: none;
        transform: translate(-50%, -50%); box-shadow: 0 0 0 2px white;
        opacity: 1; transition: opacity 0.4s ease;
      }
      .screencast-recording-click {
        position: fixed; z-index: 2147483646; width: 56px; height: 56px;
        border: 4px solid #ff3b30; border-radius: 50%; pointer-events: none;
        transform: translate(-50%, -50%); animation: screencast-click .8s ease-out;
      }
      @keyframes screencast-click {
        from { opacity: .95; transform: translate(-50%, -50%) scale(.35); }
        to { opacity: 0; transform: translate(-50%, -50%) scale(1.25); }
      }
    `;
    document.documentElement.appendChild(style);
    const cursor = document.createElement('div');
    cursor.id = 'screencast-recording-cursor';
    document.documentElement.appendChild(cursor);
    let idleTimer = null;
    const scheduleIdleFade = () => {
      if (idleTimer !== null) {
        clearTimeout(idleTimer);
      }
      idleTimer = setTimeout(() => {
        cursor.style.opacity = '0';
      }, 3000);
    };
    document.addEventListener('mousemove', event => {
      cursor.style.left = `${event.clientX}px`;
      cursor.style.top = `${event.clientY}px`;
      cursor.style.opacity = '1';
      scheduleIdleFade();
    }, true);
    // Schedule once at install time too, so a cursor that never moves at
    // all (e.g. a turn that only types or clicks without ever moving) still
    // fades out after the idle timeout instead of staying visible forever.
    scheduleIdleFade();
    document.addEventListener('click', event => {
      const click = document.createElement('div');
      click.className = 'screencast-recording-click';
      click.style.left = `${event.clientX}px`;
      click.style.top = `${event.clientY}px`;
      document.documentElement.appendChild(click);
      click.addEventListener('animationend', () => click.remove());
    }, true);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
"""

# Human-paced input timings, carried over from the three e2e_*.py scripts
# this library replaces.
MOVE_STEPS = 18
MOVE_SETTLE_MS = 450
CLICK_SETTLE_MS = 850
FILL_SETTLE_MS = 650
TYPE_DELAY_MS = 75
