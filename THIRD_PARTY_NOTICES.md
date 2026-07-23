# Third-party notices

Blink Camera AI Hub is an independent project and is not affiliated with or endorsed
by Amazon, Blink, or Immedia Semiconductor. Blink product names and trademarks
belong to their respective owners.

The project depends on third-party software. In particular:

- [BlinkPy](https://github.com/fronzbot/blinkpy) is distributed under the MIT
  License and communicates with an unofficial Blink API. Respect its request
  throttling guidance and the Blink service terms that apply to your account.
- [Ultralytics](https://github.com/ultralytics/ultralytics) code and YOLO model
  weights are offered under AGPL-3.0 or a separate Ultralytics enterprise
  license. Model weights are downloaded separately and are not committed to
  this repository.
- [Moondream2](https://huggingface.co/vikhyatk/moondream2) model code and
  weights are distributed separately under the Apache License 2.0 according
  to its model card. They are downloaded to the local Hugging Face cache and
  are not committed to this repository.
- FFmpeg is installed by the Docker image and remains subject to the license of
  the particular FFmpeg build and its enabled components.

Other Python and JavaScript dependencies retain their own copyright notices and
licenses. See `requirements.txt`, `package.json`, and `package-lock.json` for the
dependency lists.
