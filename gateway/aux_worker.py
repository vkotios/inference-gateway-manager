"""Private CPU-only inference worker; one JSON command per line on stdin."""

import json
import os
import resource
import sys

# Set before importing numerical libraries so each worker has a bounded CPU budget.
threads = os.environ.get("IGM_AUX_THREADS", "4")
os.environ["OMP_NUM_THREADS"] = threads
os.environ["MKL_NUM_THREADS"] = threads
os.environ["CUDA_VISIBLE_DEVICES"] = ""
if "IGM_AUX_MEMORY_LIMIT" in os.environ:
    limit = int(os.environ["IGM_AUX_MEMORY_LIMIT"])
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def main():
    kind, model_path = sys.argv[1:3]
    if kind == "transcription":
        from faster_whisper import WhisperModel

        model = WhisperModel(
            model_path, device="cpu", compute_type="int8", cpu_threads=int(threads)
        )
    elif kind == "embeddings":
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_path, device="cpu", local_files_only=True)
    else:
        raise ValueError("Unknown worker type")
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if kind == "embeddings":
                texts = request["input"]
                encoded = model.tokenizer(texts, truncation=False, add_special_tokens=True)
                if any(len(tokens) > model.max_seq_length for tokens in encoded["input_ids"]):
                    raise ValueError("Input exceeds model token limit")
                embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
                result = {
                    "vectors": embeddings.tolist(),
                    "tokens": sum(map(len, encoded["input_ids"])),
                }
            else:
                segments, info = model.transcribe(
                    request["path"],
                    language=request.get("language"),
                    word_timestamps=True,
                    vad_filter=True,
                )
                segments_out, words, text = [], [], []
                for segment in segments:
                    segments_out.append(
                        {
                            "id": segment.id,
                            "start": segment.start,
                            "end": segment.end,
                            "text": segment.text,
                        }
                    )
                    text.append(segment.text)
                    for word in segment.words or []:
                        words.append({"word": word.word, "start": word.start, "end": word.end})
                result = {
                    "text": "".join(text).strip(),
                    "segments": segments_out,
                    "words": words,
                    "language": info.language,
                    "duration": info.duration,
                }
            print(json.dumps({"result": result}, allow_nan=False), flush=True)
        except Exception as exc:
            error = (
                "Input exceeds model token limit"
                if isinstance(exc, ValueError) and str(exc) == "Input exceeds model token limit"
                else "Inference failed; check the model and input"
            )
            print(json.dumps({"error": error}), flush=True)


if __name__ == "__main__":
    main()
