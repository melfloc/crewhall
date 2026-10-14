"use strict";
/* Keep a scrollable box pinned to its bottom while "following".
 *
 * Following is decided ONLY by real user intent (wheel, touch, key, pointer /
 * scrollbar drag): a bare `scroll` event does not change it, because programmatic
 * scrolls and browser scroll-anchoring fire it too and would lie. While following,
 * the box stays at the bottom when content grows (streaming text, node
 * replacement, code/markdown finishing) or becomes visible; when the user scrolls
 * up it stays exactly where it is. Reaching the bottom by hand resumes following.
 *
 * Usage: const f = followBottom(box, {content, onChange}); f.toBottom();
 */
function followBottom(box, opts = {}){
  const content = opts.content || box;
  const threshold = opts.threshold || 40;
  const gestureWindow = opts.gestureWindow || 500;
  let follow = opts.follow !== false;
  let gesture = 0;
  let raf = 0;
  const nearBottom = () => box.scrollHeight - box.scrollTop - box.clientHeight <= threshold;
  const apply = () => { raf = 0; if(follow) box.scrollTop = box.scrollHeight; };
  const schedule = () => { if(!raf) raf = requestAnimationFrame(apply); };
  const notify = () => { if(opts.onChange) opts.onChange(follow); };
  const setFollow = (v) => { v = !!v; if(v === follow) return; follow = v; notify(); if(follow) schedule(); };
  const recompute = () => {
    if(Date.now() - gesture > gestureWindow) return;
    setFollow(nearBottom());
  };
  const markGesture = () => {
    gesture = Date.now();
    // Let the browser apply the scroll/keys first, then read the real position.
    requestAnimationFrame(() => requestAnimationFrame(recompute));
  };

  const gestures = ["wheel", "touchstart", "touchmove", "pointerdown", "pointermove", "keydown"];
  gestures.forEach(ev => box.addEventListener(ev, markGesture, {passive:true}));
  const onScroll = () => { if(Date.now() - gesture <= gestureWindow) recompute(); };
  box.addEventListener("scroll", onScroll, {passive:true});

  // Programmatic changes must not be re-anchored by the browser: keep the exact
  // offset the user chose (and let loadOlder compensate explicitly).
  box.style.overflowAnchor = "none";

  const ro = new ResizeObserver(() => { if(follow) schedule(); });
  ro.observe(content);
  const mo = new MutationObserver(() => { if(follow) schedule(); });
  mo.observe(content, {childList:true, subtree:true, characterData:true});
  let io = null;
  if(typeof IntersectionObserver === "function"){
    io = new IntersectionObserver(() => { if(follow) schedule(); });
    io.observe(box);
  }

  return {
    toBottom(){ if(!follow){ follow = true; notify(); } apply(); },
    refresh(){ if(follow) schedule(); },
    visible(){ if(follow) schedule(); },
    setFollow,
    isFollowing: () => follow,
    destroy(){
      ro.disconnect(); mo.disconnect(); if(io) io.disconnect();
      gestures.forEach(ev => box.removeEventListener(ev, markGesture));
      box.removeEventListener("scroll", onScroll);
    },
  };
}
