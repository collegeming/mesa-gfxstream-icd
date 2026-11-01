// Copyright 2026 The ChromiumOS Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

/// Production detach ordering, shared by virtio-gpu UNMAP, UNREF and reset.
/// No renderer operation may run when the registration outcome is unknown.
pub(super) fn detach<E>(
    offset: &mut Option<u64>,
    renderer_mapped: &mut bool,
    quarantined: bool,
    quarantine_error: impl FnOnce() -> E,
    unregister: impl FnOnce(u64) -> Result<(), E>,
    renderer_unmap: impl FnOnce() -> Result<(), E>,
) -> Result<(), E> {
    if quarantined {
        return Err(quarantine_error());
    }
    if let Some(value) = *offset {
        unregister(value)?;
        *offset = None;
    }
    if *renderer_mapped {
        renderer_unmap()?;
        *renderer_mapped = false;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[test]
    fn native_detach_ack_precedes_renderer() {
        let calls = RefCell::new(Vec::new());
        let (mut offset, mut mapped) = (Some(64), true);
        detach(&mut offset, &mut mapped, false, || "quarantine", |v| {
            assert_eq!(v, 64);
            calls.borrow_mut().push("ack");
            Ok::<_, &str>(())
        }, || { calls.borrow_mut().push("renderer"); Ok(()) }).unwrap();
        assert_eq!(*calls.borrow(), ["ack", "renderer"]);
        assert_eq!((offset, mapped), (None, false));
    }

    #[test]
    fn native_detach_unregister_error_preserves_owner_and_retry() {
        let (mut offset, mut mapped) = (Some(64), true);
        assert_eq!(detach(&mut offset, &mut mapped, false, || "quarantine",
            |_| Err("unregister"), || panic!("must retain renderer")), Err("unregister"));
        assert_eq!((offset, mapped), (Some(64), true));
        detach(&mut offset, &mut mapped, false, || "quarantine", |_| Ok::<_, &str>(()), || Ok(())).unwrap();
        assert_eq!((offset, mapped), (None, false));
    }

    #[test]
    fn native_detach_renderer_failure_retries_renderer_only() {
        let (mut offset, mut mapped) = (Some(64), true);
        assert_eq!(detach(&mut offset, &mut mapped, false, || "quarantine",
            |_| Ok(()), || Err("renderer")), Err("renderer"));
        assert_eq!((offset, mapped), (None, true));
        detach(&mut offset, &mut mapped, false, || "quarantine",
            |_| panic!("already detached"), || Ok::<_, &str>(())).unwrap();
        assert_eq!((offset, mapped), (None, false));
    }

    #[test]
    fn native_detach_ambiguous_registration_never_releases() {
        let (mut offset, mut mapped) = (Some(64), true);
        assert_eq!(detach(&mut offset, &mut mapped, true, || "quarantine",
            |_| panic!("unknown region ID"), || panic!("must retain owner")), Err("quarantine"));
        assert_eq!((offset, mapped), (Some(64), true));
    }

    #[test]
    fn native_detach_renderer_only_and_idle_are_safe() {
        let (mut offset, mut mapped) = (None, true);
        detach(&mut offset, &mut mapped, false, || "quarantine",
            |_| panic!("no registration"), || Ok::<_, &str>(())).unwrap();
        detach(&mut offset, &mut mapped, false, || "quarantine",
            |_| panic!("idle"), || panic!("idle")).unwrap();
    }
}
