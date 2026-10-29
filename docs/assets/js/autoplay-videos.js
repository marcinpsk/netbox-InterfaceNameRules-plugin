// SPDX-License-Identifier: Apache-2.0
// Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
// Play each muted recording while half of it is on the screen, and pause it when it goes away.
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const observer = new IntersectionObserver(
  (entries) => {
    for (const { target, isIntersecting } of entries) {
      // A browser can refuse to play (for example, to save power); the reader then presses Play.
      if (isIntersecting) target.play().catch(() => {});
      else target.pause();
    }
  },
  { threshold: 0.5 },
);
if (!reduceMotion) document.querySelectorAll("video[muted]").forEach((video) => observer.observe(video));
