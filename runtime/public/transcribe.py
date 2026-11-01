"""Public transcript naming helpers. Credentialed calls belong to the gateway."""
from pathlib import Path

def transcript_path(edit_dir,video,audio_track=0):
    suffix=f'.track{audio_track}' if audio_track else ''
    return Path(edit_dir)/'transcripts'/f'{Path(video).stem}{suffix}.json'

def load_api_key():
    raise RuntimeError('Use the authorized transcription gateway')
