# ly2video

Turns a LilyPond score into a vertical (9:16) video with a highlight that
follows the music.

## Installation

```bash
apt install lilypond ffmpeg fluidsynth fluid-soundfont-gm
pip install .          # or: pipx install .   (development: pip install -e .)
```

This provides the `ly2video` command. The LilyPond side, `ly2video-events.ily`,
ships with the package; `ly2video --include-path` prints the directory to pass
to `lilypond -I`.

## Usage

Leave the canonical score untouched and put a small wrapper around it with the
paper size and the event logger, for example `video.ly`:

```lilypond
\version "2.24.3"
\paper {
  paper-width = 90\mm   paper-height = 160\mm
  left-margin = 5\mm    right-margin = 5\mm
  top-margin = 5\mm     bottom-margin = 5\mm
}
\header { tagline = ##f }
\include "ly2video-events.ily"   % before the \score
\include "my-song.ly"
```

Then:

```bash
lilypond -I "$(ly2video --include-path)" \
    -fsvg -dpoint-and-click=note-event -o video video.ly
fluidsynth -ni -F video.wav -r 44100 /usr/share/sounds/sf2/FluidR3_GM.sf2 video.midi
ly2video --svg 'video*.svg' --midi video.midi \
    --note-events video-events.tsv --audio video.wav -o video.mp4
```

See `ly2video --help` for all options.

On 90 mm paper, lyrics easily run off the page, because the width of the text
drives the horizontal spacing. One step smaller helps:

```lilypond
\layout { \context { \Lyrics \override LyricText.font-size = #-1 } }
```

## How the synchronisation works

`src/ly2video/ly2video-events.ily` attaches a listener to every `Voice` and,
during the same LilyPond run, writes a `*-events.tsv` with, for each note
event, the score it belongs to, its moment, and exactly the point-and-click
coordinates that also appear in the SVG. `ly2video` uses these to match every
printed notehead to its moment, and converts that to seconds through the tempo
map of the MIDI file.

This is necessary because counting notes does not work:

* a tied note (`~`) is printed, but not attacked again in MIDI;
* when two voices collide, LilyPond shifts one notehead sideways, so "same
  beat" is not the same as "same x coordinate".

Without `--note-events`, the script falls back to clustering noteheads by x and
matching those columns to MIDI note-ons in order. That only works for scores
without the cases above, and the script refuses to render, rather than guess,
when the counts do not match.

## Files with several `\score` blocks

A file may print separate sections next to the complete piece, while the
`\midi` block is a single performance. Every `\score` starts again at moment 0,
so the event log numbers them. ly2video automatically follows the score that
spans the whole MIDI file; use `--score N` to choose one yourself.

With `--stack-systems`, only what is filmed remains: the systems of that one
score are cut out of their pages and stacked one after another on fresh
screens, without the separate sections, the prose and the page numbers.

A section printed with `\repeat volta` cannot be filmed against an
`\unfoldRepeats` MIDI: it is printed once and sounds twice. The script then
refuses, instead of making a video that drifts out of sync:

```
ly2video: error: 43 of 97 MIDI attack(s) (44%) do not land on a printed onset
(worst gap 24000 ms).  The filmed score and the \midi score are not the same
music; a \repeat that is printed once but sounds twice does this.  Film a
score with \unfoldRepeats, like the \midi one.
```

In that case, put the music in an `.ily` file and give the video its own
`\score` with `\unfoldRepeats`, so that picture and sound are the same music.
The printed score keeps its repeat signs.

## Limitations

* Staves are grouped into systems by the vertical line LilyPond draws at their
  left edge (`SystemStartBar`). Do not remove it from scores with several
  staves.
* Only staves with four or more lines are recognised, so a `RhythmicStaff` is
  not followed.
* A system narrower than 45% of the page is not recognised, which can happen
  with a piece of one or two bars or with `ragged-right`.
* Systems of one score should show the same staves; hiding empty staves
  (`\RemoveEmptyStaves`) is not reliably supported.

## License

GPL-3.0-or-later; see [LICENSE](LICENSE).
