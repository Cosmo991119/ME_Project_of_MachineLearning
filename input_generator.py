class PunctuatorInput(base_input_generator.BaseSequenceInputGenerator):
  """Reads text line by line and processes them for the punctuator task."""

  @classmethod
  def Params(cls):
    """Defaults params for PunctuatorInput."""
    p = super().Params()
    p.tokenizer = tokenizers.WpmTokenizer.Params()
    return p

  def _ProcessLine(self, line):
    """A single-text-line processor.

    Gets a string tensor representing a line of text that have been read from
    the input file, and splits it to graphemes (characters).
    We use original characters as the target labels, and the lowercased and
    punctuation-removed characters as the source labels.

    Args:
      line: a 1D string tensor.

    Returns:
      A list of tensors, in the expected order by __init__.
    """
    # Tokenize the input into integer ids.
    # tgt_ids has the start-of-sentence token prepended, and tgt_labels has the
    # end-of-sentence token appended.
    tgt_ids, tgt_labels, tgt_paddings = self.StringsToIds(
        tf.convert_to_tensor([line]))

    def Normalize(line):
      # Lowercase and remove punctuation.
      line = line.lower().translate(None, string.punctuation.encode('utf-8'))
      # Convert multiple consecutive spaces to a single one.
      line = b' '.join(line.split())
      return line

    normalized_line = tf.py_func(Normalize, [line], tf.string, stateful=False)
    _, src_labels, src_paddings = self.StringsToIds(
        tf.convert_to_tensor([normalized_line]), is_source=True)
    # The model expects the source without a start-of-sentence token.
    src_ids = src_labels

    # Compute the length for bucketing.
    bucket_key = tf.cast(
        tf.round(
            tf.maximum(
                tf.reduce_sum(1.0 - src_paddings),
                tf.reduce_sum(1.0 - tgt_paddings))), tf.int32)
    tgt_weights = 1.0 - tgt_paddings

    # Return tensors in an order consistent with __init__.
    out_tensors = [
        src_ids, src_paddings, tgt_ids, tgt_paddings, tgt_labels, tgt_weights
    ]
    return [tf.squeeze(t, axis=0) for t in out_tensors], bucket_key

  def _DataSourceFromFilePattern(self, file_pattern):
    """Create the input processing op.

    Args:
      file_pattern: The file pattern to use as input.

    Returns:
      an operation that when executed, calls `_ProcessLine` on a line read
    from `file_pattern`.
    """
    ret = py_utils.NestedMap()

    (src_ids, src_paddings, tgt_ids, tgt_paddings, tgt_labels,
     tgt_weights), ret.bucket_keys = generic_input.GenericInput(
         file_pattern=file_pattern,
         processor=self._ProcessLine,
         # Pad dimension 0 to the same length.
         dynamic_padding_dimensions=[0] * 6,
         # The constant values to use for padding each of the outputs.
         dynamic_padding_constants=[0, 1, 0, 1, 0, 0],
         **self.CommonInputOpArgs())

    ret.src = py_utils.NestedMap()
    ret.src.ids = tf.cast(src_ids, dtype=tf.int32)
    ret.src.paddings = src_paddings

    ret.tgt = py_utils.NestedMap()
    ret.tgt.ids = tgt_ids
    ret.tgt.labels = tf.cast(tgt_labels, dtype=tf.int32)
    ret.tgt.weights = tgt_weights
    ret.tgt.paddings = tgt_paddings

    return ret
