"""macOS FindMyLocate bridge and contact enrichment for BlueBubbles FindMy Friends.

On macOS Sonoma (14.0) and newer, Apple replaced ``FMFSession`` in ``IMCore.framework``
with ``FindMyLocateSession``. BlueBubbles Server v1.9.x skips ``privateApi.findmy.refreshFriends()``
on macOS 14+ and leaves ``-[BBH_FindMyLocateSession locationUpdateCallback]`` as a no-op stub,
causing ``GET /api/v1/icloud/findmy/friends`` and ``POST /api/v1/icloud/findmy/friends/refresh``
to return ``[]``.

When running locally on macOS alongside BlueBubbles, this module compiles and runs a small
Objective-C helper that queries ``FindMyLocateSession`` (and ``MKReverseGeocodingRequest`` /
``CLGeocoder`` for street-level reverse geocoding), enriches handles with contact names from
BlueBubbles ``/api/v1/contact``, and pushes the resulting ``new-findmy-location`` event into
BlueBubbles's Private API TCP socket (``localhost:45670``) so ``Server().findMyCache`` stays warm.
"""

from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DIGITS_RE = re.compile(r"\D+")

_FINDMY_ENTITLEMENTS_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.findmy.findmylocate.friendshipservice</key>
    <true/>
    <key>com.apple.findmy.findmylocate.locationservice</key>
    <true/>
    <key>com.apple.findmy.findmylocate.settings</key>
    <true/>
    <key>com.apple.icloud.fmfd</key>
    <true/>
</dict>
</plist>
"""

_FINDMY_HELPER_OBJC = r"""
#import <Foundation/Foundation.h>
#import <CoreLocation/CoreLocation.h>
#import <MapKit/MapKit.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <dlfcn.h>

static NSString *statusFromLocationType(long locType) {
    if (locType == 2) return @"live";
    if (locType == 1) return @"shallow";
    return @"legacy";
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        dlopen("/System/Library/PrivateFrameworks/IMCore.framework/IMCore", RTLD_NOW);
        Class imfmfCls = objc_getClass("IMFMFSession");
        if (!imfmfCls) {
            printf("[]\n");
            return 0;
        }
        id shared = ((id(*)(id, SEL))objc_msgSend)(imfmfCls, sel_registerName("sharedInstance"));
        Class fmlCls = shared ? ((Class(*)(id, SEL))objc_msgSend)(shared, sel_registerName("__FMLSessionClass")) : nil;
        if (!fmlCls) {
            printf("[]\n");
            return 0;
        }
        id fml = ((id(*)(id, SEL))objc_msgSend)(((id(*)(id, SEL))objc_msgSend)(fmlCls, sel_registerName("alloc")), sel_registerName("init"));
        if (!fml) {
            printf("[]\n");
            return 0;
        }

        dispatch_semaphore_t sem = dispatch_semaphore_create(0);
        NSMutableArray *handles = [NSMutableArray array];
        void (^ friendBlock)(NSArray *, NSError *) = ^(NSArray *friends, NSError *err) {
            for (id f in friends) {
                id h = ((id(*)(id, SEL))objc_msgSend)(f, sel_registerName("handle"));
                if (h) [handles addObject:h];
            }
            dispatch_semaphore_signal(sem);
        };
        ((void(*)(id, SEL, id))objc_msgSend)(fml, sel_registerName("getFriendsSharingLocationsWithMeWithCompletion:"), friendBlock);
        dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC));

        NSMutableDictionary *locsByHandle = [NSMutableDictionary dictionary];
        void (^ locCallback)(id, id) = ^(id location, id handle) {
            NSString *ident = ((id(*)(id, SEL))objc_msgSend)(handle, sel_registerName("identifier"));
            if (ident && location) {
                @synchronized (locsByHandle) {
                    locsByHandle[ident] = location;
                }
            }
        };
        ((void(*)(id, SEL, id))objc_msgSend)(fml, sel_registerName("setLocationUpdateCallback:"), locCallback);

        void (^ refreshBlock)(NSError *) = ^(NSError *err) {
            dispatch_semaphore_signal(sem);
        };
        if (handles.count > 0) {
            ((void(*)(id, SEL, id, long, BOOL, BOOL, id))objc_msgSend)(
                fml,
                sel_registerName("startRefreshingLocationForHandles:priority:isFromGroup:reverseGeocode:completion:"),
                handles, 1, NO, YES, refreshBlock
            );
            dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC));

            NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:4.0];
            while ([deadline timeIntervalSinceNow] > 0) {
                @synchronized (locsByHandle) {
                    if (locsByHandle.count >= handles.count) break;
                }
                [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
            }
        }

        NSMutableDictionary *geoLongByHandle = [NSMutableDictionary dictionary];
        NSMutableDictionary *geoShortByHandle = [NSMutableDictionary dictionary];
        __block NSInteger pendingGeo = 0;
        if (@available(macOS 26.0, *)) {
            for (id h in handles) {
                NSString *ident = ((id(*)(id, SEL))objc_msgSend)(h, sel_registerName("identifier"));
                if (!ident) continue;
                id loc = locsByHandle[ident];
                if (!loc) continue;
                double lat = ((double(*)(id, SEL))objc_msgSend)(loc, sel_registerName("latitude"));
                double lon = ((double(*)(id, SEL))objc_msgSend)(loc, sel_registerName("longitude"));
                if (lat == 0.0 && lon == 0.0) continue;
                CLLocation *clLoc = [[CLLocation alloc] initWithLatitude:lat longitude:lon];
                MKReverseGeocodingRequest *req = [[MKReverseGeocodingRequest alloc] initWithLocation:clLoc];
                if (!req) continue;
                pendingGeo++;
                [req getMapItemsWithCompletionHandler:^(NSArray<MKMapItem *> *mapItems, NSError *error) {
                    MKMapItem *mi = mapItems.firstObject;
                    #pragma clang diagnostic push
                    #pragma clang diagnostic ignored "-Wdeprecated-declarations"
                    NSString *pmTitle = mi.placemark.title;
                    NSString *locality = mi.placemark.locality;
                    NSString *admin = mi.placemark.administrativeArea;
                    #pragma clang diagnostic pop
                    @synchronized (geoLongByHandle) {
                        if (pmTitle.length > 0) geoLongByHandle[ident] = pmTitle;
                        if (mi.name.length > 0 && locality.length > 0 && admin.length > 0) {
                            geoShortByHandle[ident] = [NSString stringWithFormat:@"%@, %@, %@", mi.name, locality, admin];
                        } else if (locality.length > 0 && admin.length > 0) {
                            geoShortByHandle[ident] = [NSString stringWithFormat:@"%@, %@", locality, admin];
                        }
                        pendingGeo--;
                    }
                }];
            }
            NSDate *geoDeadline = [NSDate dateWithTimeIntervalSinceNow:3.0];
            while ([geoDeadline timeIntervalSinceNow] > 0) {
                @synchronized (geoLongByHandle) {
                    if (pendingGeo <= 0) break;
                }
                [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
            }
        }

        NSMutableArray *results = [NSMutableArray array];
        for (id h in handles) {
            NSString *ident = ((id(*)(id, SEL))objc_msgSend)(h, sel_registerName("identifier"));
            if (!ident) continue;
            id loc = nil;
            @synchronized (locsByHandle) {
                loc = locsByHandle[ident];
            }
            if (!loc) {
                loc = ((id(*)(id, SEL, id, BOOL))objc_msgSend)(fml, sel_registerName("cachedLocationForHandle:includeAddress:"), h, YES);
            }
            if (!loc) continue;

            double lat = ((double(*)(id, SEL))objc_msgSend)(loc, sel_registerName("latitude"));
            double lon = ((double(*)(id, SEL))objc_msgSend)(loc, sel_registerName("longitude"));
            double ts = ((double(*)(id, SEL))objc_msgSend)(loc, sel_registerName("timestamp"));
            id coarse = ((id(*)(id, SEL))objc_msgSend)(loc, sel_registerName("coarseAddressLabel"));
            long locType = ((long(*)(id, SEL))objc_msgSend)(loc, sel_registerName("locationType"));
            id addr = ((id(*)(id, SEL))objc_msgSend)(loc, sel_registerName("address"));
            NSArray *lines = addr ? ((id(*)(id, SEL))objc_msgSend)(addr, sel_registerName("formattedAddressLines")) : nil;

            NSString *fmlLong = (lines && lines.count > 0) ? [lines componentsJoinedByString:@", "] : coarse;
            NSString *fmlShort = coarse ?: ((lines && lines.count > 0) ? lines.firstObject : nil);

            NSString *longAddr = geoLongByHandle[ident] ?: fmlLong;
            NSString *shortAddr = geoShortByHandle[ident] ?: fmlShort;
            long long lastUpdatedMs = (long long)(ts * 1000.0);

            NSDictionary *item = @{
                @"handle": ident,
                @"coordinates": @[@(lat), @(lon)],
                @"long_address": longAddr ?: [NSNull null],
                @"short_address": shortAddr ?: [NSNull null],
                @"subtitle": fmlShort ?: (shortAddr ?: [NSNull null]),
                @"title": [NSNull null],
                @"last_updated": @(lastUpdatedMs),
                @"is_locating_in_progress": @0,
                @"status": statusFromLocationType(locType)
            };
            [results addObject:item];
        }

        NSData *jsonData = [NSJSONSerialization dataWithJSONObject:results options:0 error:nil];
        NSString *jsonStr = [[NSString alloc] initWithData:jsonData encoding:NSUTF8StringEncoding];
        printf("%s\n", [jsonStr UTF8String]);
    }
    return 0;
}
"""


def _helper_cache_dir() -> Path:
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    candidate = Path(hermes_home) / "cache" / "bluebubbles"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        fallback = Path(tempfile.gettempdir()) / f"hermes-bluebubbles-{os.getuid()}"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def ensure_findmy_helper_binary() -> Optional[str]:
    """Compile and ad-hoc sign the FindMyLocate helper on macOS if needed."""
    if sys.platform != "darwin":
        return None
    digest = hashlib.sha256(
        (_FINDMY_HELPER_OBJC + _FINDMY_ENTITLEMENTS_PLIST).encode("utf-8")
    ).hexdigest()[:12]
    cache_dir = _helper_cache_dir()
    bin_path = cache_dir / f"findmy_locate_helper_{digest}"
    if bin_path.is_file() and os.access(bin_path, os.X_OK):
        return str(bin_path)

    src_path = cache_dir / f"findmy_locate_helper_{digest}.m"
    ent_path = cache_dir / f"findmy_locate_helper_{digest}.entitlements.plist"
    tmp_bin = cache_dir / f"findmy_locate_helper_{digest}.tmp.{os.getpid()}"
    try:
        src_path.write_text(_FINDMY_HELPER_OBJC, encoding="utf-8")
        ent_path.write_text(_FINDMY_ENTITLEMENTS_PLIST, encoding="utf-8")
        subprocess.run(
            [
                "/usr/bin/clang",
                "-O2",
                "-framework",
                "Foundation",
                "-framework",
                "CoreLocation",
                "-framework",
                "MapKit",
                str(src_path),
                "-o",
                str(tmp_bin),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            [
                "/usr/bin/codesign",
                "-f",
                "-s",
                "-",
                "--entitlements",
                str(ent_path),
                str(tmp_bin),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        os.replace(tmp_bin, bin_path)
        return str(bin_path)
    except Exception as exc:
        logger.debug("[bluebubbles] failed to build FindMyLocate helper: %s", exc)
        with suppress(OSError):
            tmp_bin.unlink(missing_ok=True)
        return None


def push_findmy_locations_to_bluebubbles(
    items: list[dict[str, Any]], port: Optional[int] = None
) -> bool:
    """Push ``new-findmy-location`` into BlueBubbles's Private API socket so its cache stays warm."""
    if not items:
        return False
    target_port = port or max(45670, min(65535, 45670 + os.getuid() - 501))
    payload = (json.dumps({"event": "new-findmy-location", "data": items}) + "\n").encode("utf-8")
    for host in ("localhost", "127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, target_port), timeout=2.0) as sock:
                sock.sendall(payload)
            return True
        except OSError:
            continue
    return False


def fetch_macos_findmy_friends(push_to_bluebubbles: bool = True) -> list[dict[str, Any]]:
    """Run the native FindMyLocate helper and return a list of FindMyLocationItem dicts."""
    bin_path = ensure_findmy_helper_binary()
    if not bin_path:
        return []
    try:
        proc = subprocess.run(
            [bin_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        items = [item for item in parsed if isinstance(item, dict) and item.get("handle")]
        if push_to_bluebubbles and items:
            push_findmy_locations_to_bluebubbles(items)
        return items
    except Exception as exc:
        logger.debug("[bluebubbles] FindMyLocate helper execution failed: %s", exc)
        return []


def normalize_contact_key(address: Optional[str]) -> str:
    """Normalize a phone number or email address for contact-to-FindMy-handle matching."""
    raw = (address or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw.lower()
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits or raw.lower()


def build_contact_name_map(contacts: list[dict[str, Any]]) -> dict[str, str]:
    """Build a normalized handle -> display name map from BlueBubbles ``/api/v1/contact`` records."""
    by_key: dict[str, str] = {}
    for contact in contacts or []:
        if not isinstance(contact, dict):
            continue
        first = (contact.get("firstName") or "").strip()
        last = (contact.get("lastName") or "").strip()
        name = (
            (contact.get("displayName") or "").strip()
            or f"{first} {last}".strip()
            or (contact.get("nickname") or "").strip()
        )
        if not name:
            continue
        for phone in contact.get("phoneNumbers") or []:
            addr = phone.get("address") if isinstance(phone, dict) else str(phone or "")
            if key := normalize_contact_key(addr):
                by_key.setdefault(key, name)
        for email in contact.get("emails") or []:
            addr = email.get("address") if isinstance(email, dict) else str(email or "")
            if key := normalize_contact_key(addr):
                by_key.setdefault(key, name)
    return by_key


def enrich_and_filter_findmy_friends(
    items: list[dict[str, Any]],
    contact_map: dict[str, str],
    query_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Attach contact ``display_name`` / ``title`` to FindMy items and optionally filter by name/handle."""
    enriched: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        handle = str(entry.get("handle") or "").strip()
        name = contact_map.get(normalize_contact_key(handle))
        if name:
            entry["display_name"] = name
            if not entry.get("title"):
                entry["title"] = name
        elif entry.get("title") and not entry.get("display_name"):
            entry["display_name"] = entry["title"]
        enriched.append(entry)

    q = (query_filter or "").strip()
    if not q:
        return enriched

    q_lower = q.lower()
    q_key = normalize_contact_key(q)
    matched: list[dict[str, Any]] = []
    for entry in enriched:
        handle = str(entry.get("handle") or "")
        display_name = str(entry.get("display_name") or "")
        title = str(entry.get("title") or "")
        if q_key and normalize_contact_key(handle) == q_key:
            matched.append(entry)
            continue
        if (
            q_lower in handle.lower()
            or q_lower in display_name.lower()
            or q_lower in title.lower()
        ):
            matched.append(entry)
    return matched
