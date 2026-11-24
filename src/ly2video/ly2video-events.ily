%% SPDX-License-Identifier: GPL-3.0-or-later
%% Copyright (C) 2026 Erik Nijenhuis
%% ly2video-events.ily -- log every note-event with its musical moment.
%%
%% Include this BEFORE the score you want to analyse:
%%
%%     \include "ly2video-events.ily"
%%     \include "my-score.ly"
%%
%% It installs a listener in every Voice of every \layout score and writes
%% "<output-basename>-events.tsv" next to LilyPond's other output files:
%%
%%     score<TAB>file<TAB>line<TAB>char<TAB>column<TAB>moment<TAB>midi-pitch
%%
%% "score" numbers the \layout scores of the file in document order, because
%% every \score starts counting moments again at 0.
%%
%% "moment" is the score time in whole notes (1.0 == one whole note), with the
%% grace-note offset already folded in, so grace notes sort just before the beat
%% they decorate.  file/line/char/column are exactly the coordinates LilyPond
%% puts in its point-and-click textedit:// links, which is what lets ly2video
%% pair every notehead in the SVG with the moment it sounds.
%%
%% Note-events are logged once per Voice context, so a score that prints the
%% same music on both a Staff and a TabStaff logs every event twice.  Consumers
%% are expected to de-duplicate on the whole line.

\version "2.24.3"

#(define ly2video-events-port #f)
#(define ly2video-score-table '())

#(define (ly2video-score-index context)
   "Position of this context's Score in the file, counting only \\layout scores.

Every \\score restarts the clock at moment 0, so a moment on its own does not
identify a note in a file that holds several scores.  Scores are interpreted in
document order, which is also the order they are printed in, so a running
counter keyed on the Score context is enough to keep them apart."
   (let* ((score (ly:context-find context 'Score))
          (hit (assq score ly2video-score-table)))
     (if hit
         (cdr hit)
         (let ((index (length ly2video-score-table)))
           (set! ly2video-score-table
                 (cons (cons score index) ly2video-score-table))
           index))))

#(define (ly2video-events-output-port)
   (or ly2video-events-port
       (let ((port (open-output-file
                    (string-append (ly:parser-output-name) "-events.tsv"))))
         (display "score\tfile\tline\tchar\tcolumn\tmoment\tpitch\n" port)
         (set! ly2video-events-port port)
         port)))

#(define (ly2video-moment->number moment)
   ;; Main score time plus the (negative) grace offset, as a single number.
   (+ (/ (ly:moment-main-numerator moment)
         (ly:moment-main-denominator moment))
      (if (zero? (ly:moment-grace-numerator moment))
          0
          (/ (ly:moment-grace-numerator moment)
             (ly:moment-grace-denominator moment)))))

#(define (ly2video-log-note engraver event)
   (let* ((context (ly:translator-context engraver))
          (origin (ly:input-file-line-char-column (ly:event-property event 'origin)))
          (pitch (ly:event-property event 'pitch))
          (port (ly2video-events-output-port)))
     (display
      (format #f "~a\t~a\t~a\t~a\t~a\t~a\t~a\n"
              (ly2video-score-index context)
              (car origin) (cadr origin) (caddr origin) (cadddr origin)
              (exact->inexact
               (ly2video-moment->number (ly:context-current-moment context)))
              (if (ly:pitch? pitch) (+ 60 (ly:pitch-semitones pitch)) -1))
      port)
     ;; LilyPond may die before Guile flushes its ports; keep the file usable.
     (force-output port)))

videoEventLogger =
#(make-engraver
  (listeners
   ((note-event engraver event) (ly2video-log-note engraver event))))

\layout {
  \context {
    \Voice
    \consists #videoEventLogger
  }
  %% Cue notes (\new CueVoice) are printed and sound in the MIDI too, but
  %% CueVoice is its own context definition, so it needs the logger as well.
  \context {
    \CueVoice
    \consists #videoEventLogger
  }
}
