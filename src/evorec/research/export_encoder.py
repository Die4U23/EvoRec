"""Research-only export of the hash-pinned, already frozen R06 text encoder.

Legacy joblib is executable and requires explicit acknowledgement. Only the
verified bytes of the trusted local research artifact are deserialized. Service
loading uses JSON/float32 instead; this does not fit or evaluate any model.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import unicodedata

import joblib
import numpy as np
import scipy
import sklearn
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from evorec.infrastructure.content_encoder import load_content_encoder, text_protocol, VECTOR_TOLERANCE
from evorec.infrastructure.r06_features import load_r06_features
from evorec.research.content import ContentFeatures
from evorec.research.export_ranker import _checked, _json_bytes, _read, _record, _sha, _snapshot_code

MAX_LEGACY_BYTES = 64 * 1024 * 1024


def _check_transform(vectorizer, svd):
    if type(vectorizer) is not TfidfVectorizer or type(svd) is not TruncatedSVD:
        raise ValueError("only the fixed sklearn TF-IDF/SVD classes are supported")
    params = vectorizer.get_params()
    expected = {"analyzer": "word", "binary": False, "decode_error": "strict", "dtype": np.float32,
                "encoding": "utf-8", "input": "content", "lowercase": True, "ngram_range": (1, 2),
                "norm": "l2", "preprocessor": None, "smooth_idf": True, "stop_words": None,
                "strip_accents": None, "sublinear_tf": True, "token_pattern": text_protocol()["token_pattern"],
                "tokenizer": None, "use_idf": True, "vocabulary": None,
                "max_features": 20000, "min_df": 2, "max_df": 1.0}
    if (set(params) != set(expected)
            or any(type(params[k]) is not type(v) or params[k] != v for k, v in expected.items())):
        raise ValueError("text transform differs from the frozen protocol")
    if (vectorizer.idf_.dtype != np.float32 or svd.components_.dtype != np.float32
            or svd.components_.shape != (svd.n_components, len(vectorizer.vocabulary_))
            or svd.n_features_in_ != len(vectorizer.vocabulary_)):
        raise ValueError("encoder dtype or fitted shape differs")
    indices = list(vectorizer.vocabulary_.values())
    if (any(isinstance(i, (bool, np.bool_)) or not isinstance(i, (int, np.integer)) for i in indices)
            or sorted(int(i) for i in indices) != list(range(len(indices)))):
        raise ValueError("vocabulary indices must be contiguous")


def write_encoder(output, vectorizer, svd, provenance, fit, samples):
    _check_transform(vectorizer, svd)
    terms = vectorizer.get_feature_names_out().tolist()
    vocabulary = _json_bytes(terms)
    weights = (vectorizer.idf_.astype("<f4").tobytes()
               + svd.components_.T.astype("<f4").tobytes(order="C"))
    validation = _json_bytes(samples)
    manifest = {
        "schema_version": 1, "kind": "frozen-tfidf-svd-v1", "dtype": "float32-le",
        "dimension": svd.n_components, "vocabulary_terms": len(terms), "text_protocol": text_protocol(),
        "provenance": provenance, "fit": fit, "vocabulary": _record("vocabulary.json", vocabulary),
        "weights": _record("weights.f32", weights), "validation": _record("validation.json", validation),
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        for name, raw in (("vocabulary.json", vocabulary), ("weights.f32", weights), ("validation.json", validation)):
            (output / name).write_bytes(raw)
        (output / "manifest.json").write_bytes(_json_bytes(manifest))
        return load_content_encoder(output)
    except BaseException:
        (output / "manifest.json").unlink(missing_ok=True)
        raise


def _trusted_model(path, expected_hash):
    if path.stat().st_size > MAX_LEGACY_BYTES:
        raise ValueError("legacy encoder exceeds byte limit")
    with path.open("rb") as stream:
        raw = stream.read(MAX_LEGACY_BYTES + 1)
    if len(raw) > MAX_LEGACY_BYTES or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("legacy encoder bytes differ from the frozen hash")
    model = joblib.load(io.BytesIO(raw))  # Never deserialize a different file after checking its hash.
    if not isinstance(model, dict) or set(model) != {"vectorizer", "svd"}:
        raise ValueError("legacy encoder contains unexpected objects")
    _check_transform(model["vectorizer"], model["svd"])
    return model


def _finish_export(output, report, model, provenance, fit, samples, details, features_component=None,
                   expected_features_manifest_sha256=None):
    report = Path(report)
    if report.exists():
        raise FileExistsError("encoder verification report already exists")
    runtime = write_encoder(output, model["vectorizer"], model["svd"], provenance, fit, samples)
    report_owned = False
    try:
        if features_component is not None:
            runtime.check_features(load_r06_features(
                features_component, expected_manifest_sha256=expected_features_manifest_sha256))
        error = max(abs(a - b) for sample in samples for a, b in
                    zip(runtime.encode(sample["text"]), sample["expected_vector"], strict=True))
        result = {**details, "status": "passed", "component_only": True, "activated": False,
                  "manifest_sha256": runtime.manifest_sha256, "dimension": runtime.dimension,
                  "vocabulary_terms": runtime.vocabulary_terms, "provenance": provenance, "fit": fit,
                  "validation_samples": len(samples), "max_absolute_vector_error": error,
                  "vector_tolerance": VECTOR_TOLERANCE, "feature_binding_checked": features_component is not None,
                  "retrained": False, "test_queries_evaluated": False, "retrieval_recomputed": False}
        with report.open("xb") as stream:
            report_owned = True
            stream.write(_json_bytes(result))
        return result
    except BaseException:
        (Path(output) / "manifest.json").unlink(missing_ok=True)
        if report_owned:
            report.unlink(missing_ok=True)
        raise


def export(project, output, *, allow_trusted_joblib=False, features_component=None,
           expected_features_manifest_sha256=None):
    if allow_trusted_joblib is not True:
        raise ValueError("explicit acknowledgement is required for trusted research-only joblib")
    if (features_component is None) != (expected_features_manifest_sha256 is None):
        raise ValueError("feature component and approved digest must be supplied together")
    project, output = Path(project).resolve(), Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("encoder export must use a new artifacts subdirectory")
    snapshot, report = output.with_name(output.name + "-source"), output.with_name(output.name + "-verification.json")
    if any(path.exists() for path in (output, snapshot, report)):
        raise FileExistsError("encoder export destination already exists")
    archive = project / "docs/experiments/archive/r06-multi-interest-20260917.json"
    series = _read(archive)
    config = series["configuration"]
    if (series["status"] != "completed" or series["selected_method"] != "A-frozen-s17"
            or config["stage"] != "R06-multi-interest"
            or config["content"]["fields"] != ["title", "categories"]
            or config["content"]["max_features"] != 20000 or config["content"]["min_df"] != 2
            or config["content"]["svd_seed"] != 17):
        raise ValueError("not the validation-selected frozen R06 arm")
    source = project / config["source_run"]
    original = _read(_checked(project, {"path_from_project_root": str(source / "series.json"),
                                       "sha256": config["source_series_sha256"]}))
    encoder = original["content_encoder"]
    metadata_record = original["metadata_provenance"]
    if (original["status"] != "completed" or original["feature_fingerprint"] != series["feature_fingerprint"]
            or original["configuration"]["content"] != config["content"]
            or metadata_record["metadata_sha256"] != series["metadata_provenance"]["metadata_sha256"]
            or metadata_record["fields"] != ["title", "categories"]
            or metadata_record["access_assumption"] != config["content"]["access_assumption"]
            or not encoder["encoder_reload_verified"] or encoder["fit_end_ms"] > config["train_end_ms"]):
        raise ValueError("frozen encoder source or metadata protocol differs")
    paths = {name: _checked(project, {"path_from_project_root": str(source / "content-encoder" / name),
                                     "sha256": encoder["files"][name]})
             for name in ("encoder.joblib", "items.json", "vectors.npy")}
    metadata = _read(_checked(project, {"path_from_project_root": original["configuration"]["metadata_path"],
                                       "sha256": metadata_record["metadata_sha256"]}))
    model = _trusted_model(paths["encoder.joblib"], encoder["files"]["encoder.joblib"])
    features = ContentFeatures(_read(paths["items.json"]), np.load(paths["vectors.npy"], allow_pickle=False))
    if (features.fingerprint != series["feature_fingerprint"]
            or features.vectors.shape[1] != model["svd"].n_components
            or config["content"]["dimensions"] != model["svd"].n_components
            or encoder["vocabulary_terms"] != len(model["vectorizer"].vocabulary_)
            or encoder["dimensions"] != model["svd"].n_components):
        raise ValueError("encoder shape or frozen item features differ")
    # Fixed catalog positions and synthetic edge cases, never selected by targets or metrics.
    rows = sorted({0, len(features.items)//4, len(features.items)//2, 3*len(features.items)//4, len(features.items)-1})
    terms = model["vectorizer"].get_feature_names_out()
    first = str(terms[0])
    texts = [metadata.get(features.items[row], "") for row in rows]
    texts += ["", "evorecsyntheticoovzzzzzz", first, first.upper() + " " + first + " " + first,
              "CAFÉ cafe\u0301 你好 ＡＢ underscores_123 " + first, "\t\n" + first + "---" + first]
    references = normalize(model["svd"].transform(model["vectorizer"].transform(texts))).astype(np.float32)
    catalog_error = float(np.max(np.abs(references[:len(rows)] - features.vectors[rows])))
    if catalog_error > VECTOR_TOLERANCE:
        raise ValueError("frozen encoder no longer reproduces stored catalog vectors")
    samples = [{"text": text, "expected_vector": vector.tolist()} for text, vector in zip(texts, references, strict=True)]
    provenance = {
        "selected_method": "A-frozen-s17", "selection_series_sha256": _sha(archive),
        "source_series_sha256": config["source_series_sha256"], "feature_fingerprint": features.fingerprint,
        "encoder_sha256": encoder["files"]["encoder.joblib"], "items_sha256": encoder["files"]["items.json"],
        "vectors_sha256": encoder["files"]["vectors.npy"], "metadata_sha256": metadata_record["metadata_sha256"],
        "fit_item_set_sha256": encoder["fit_item_set_sha256"], "fit_training_signature": encoder["fit_training_signature"],
        "training_sample_sha256": original["model_training_provenance"]["sample_sha256"],
        "selection_protocol_id": series["protocol_id"], "training_protocol_id": original["protocol_id"],
        "encoder_protocol_id": encoder["protocol_id"],
    }
    fit = {"end_ms": encoder["fit_end_ms"], "training_rows": encoder["fit_training_rows"],
           "document_count": encoder["fit_document_count"]}
    code = _snapshot_code(snapshot)
    for relative in ("src/evorec/infrastructure/content_encoder.py", "src/evorec/infrastructure/r06_features.py",
                     "src/evorec/research/export_encoder.py", "scripts/load_content_encoder.py",
                     "tests/test_content_encoder_runtime.py", "tests/test_content_encoder_export.py"):
        raw = (Path(__file__).resolve().parents[3] / relative).read_bytes()
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        code["source_sha256"][relative] = hashlib.sha256(raw).hexdigest()
    code.update(sklearn_version=sklearn.__version__, scipy_version=scipy.__version__,
                joblib_version=joblib.__version__, unicode_version=unicodedata.unidata_version)
    return _finish_export(output, report, model, provenance, fit, samples,
                          {"catalog_rows": rows, "max_stored_catalog_vector_error": catalog_error,
                           "export_code": code, "legacy_load": "hash-pinned research-only joblib"},
                          features_component, expected_features_manifest_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--allow-trusted-joblib", action="store_true")
    parser.add_argument("--features-component", type=Path)
    parser.add_argument("--expected-features-manifest-sha256")
    args = parser.parse_args(argv)
    print(json.dumps(export(args.project_root, args.output, allow_trusted_joblib=args.allow_trusted_joblib,
                            features_component=args.features_component,
                            expected_features_manifest_sha256=args.expected_features_manifest_sha256), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
