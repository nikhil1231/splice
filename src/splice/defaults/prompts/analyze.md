Analyse the video at `{video_path}`.

This is {footage}. Your job is to index it so a specific moment can be found
again months later without scrubbing.

{chunk_note}

Break the clip into **meaningful moments** — one per topic, story, question, or
distinct action. A moment ends when the subject changes.

Prefer finer splits over coarser ones. If two consecutive stretches would earn
different titles, they are two segments, even if they are only ten seconds
apart. Ranges are trivial to merge later in the editor and painful to split.
A question and its answer belong together; two different questions do not.
Never emit fixed-length buckets, and never merge two stories into one segment.

For each segment:

- `start` / `end`: seconds{relative_note}. Be precise; these drive the edit.
- `title`: a short, specific, memorable name. Good: "₹7 train to Bombay",
  "Grandma teases Grandpa for being a hero". Bad: "Conversation", "Part 2".
- `summary`: one or two sentences of English describing what happens.
- `konkani`: **verbatim** {written} of what is actually said — whatever
  language is spoken, despite the field's name. Transcribe the sounds as
  spoken — do not translate here, and do not clean up the grammar. Leave empty
  only if nobody speaks. Where speech mixes in other languages, transcribe it
  as heard.
- `english`: a natural English translation of that speech. If it was spoken in
  English, repeat it.
- `lines`: the same speech split into subtitle-sized utterances, each with its
  own `start` and `end`{relative_note}, `kok` (the original, as in `konkani`)
  and `en` (the translation). Keep each line short enough to read on screen —
  roughly one breath, under about 12 words. These become burned-in subtitles,
  so their timing must track the actual speech.

  Also give each line a `speaker`: {speakers}
- `tags`: use these controlled values wherever they fit, written as
  `namespace:value`:

{taxonomy}

  Add `era:` for the period a story is *set* in, not when it was filmed. Use
  `quality:silent` for segments with no speech and `quality:reel_candidate` for
  anything that would stand on its own as a reel. Only invent a new tag when
  nothing above fits.

{legacy_hint}

Watch and listen to the whole clip before answering. Return only the structured
result.
