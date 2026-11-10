//
// BlueBubblesEmojiTapback.m
// Runtime swizzle for BlueBubblesHelper.dylib on macOS Sequoia / Tahoe (15+) enabling:
//   1. Arbitrary Emoji Tapbacks (IMAssociatedMessageTypeAcknowledgmentEmoji = 2006 / 3006)
//   2. Inline Text Formatting (bold, italic, underline, strikethrough) & Animated Text Effects (1..12)
//   3. Native "Send Later" Scheduled iMessages (scheduleType = 2, scheduleState = 1)
//   4. Native iMessage Polls (create, vote, add_option via com.apple.messages.Polls)
//   5. Sticker Tapbacks (IMStickerTapback = 2007 / 3007) & Freeform Sticker Placement (1000 / standalone)
//

#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <os/log.h>
#import <pthread.h>

static const long long kIMAssociatedMessageTypeSticker = 1000;
static const long long kIMAssociatedMessageTypeEmojiAdded = 2006;
static const long long kIMAssociatedMessageTypeEmojiRemoved = 3006;

static NSString * const kCustomEffectPrefix = @"__bb_custom:";
static NSString * const kPollBalloonBundleID =
    @"com.apple.messages.MSMessageExtensionBalloonPlugin:0000000000:com.apple.messages.Polls";

static pthread_mutex_t g_emojiMutex = PTHREAD_MUTEX_INITIALIZER;
static NSString *g_pendingEmoji = nil;

static IMP g_origParseReactionType = NULL;
static IMP g_origReactionToVerb = NULL;
static IMP g_origInitWithSender = NULL;
static IMP g_origHandleMessage = NULL;

// ---------------------------------------------------------------------------
// 1. Arbitrary Emoji Tapbacks (2006 / 3006)
// ---------------------------------------------------------------------------

static BOOL isClassicReaction(NSString *reactionType) {
    if (!reactionType || ![reactionType isKindOfClass:[NSString class]]) {
        return NO;
    }
    static NSSet<NSString *> *classicSet = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        classicSet = [NSSet setWithArray:@[
            @"love", @"like", @"dislike", @"laugh", @"emphasize", @"question",
            @"-love", @"-like", @"-dislike", @"-laugh", @"-emphasize", @"-question"
        ]];
    });
    return [classicSet containsObject:[reactionType lowercaseString]];
}

static NSString *extractEmojiAndMode(NSString *reactionType, BOOL *outIsRemoved) {
    if (outIsRemoved) {
        *outIsRemoved = NO;
    }
    if (!reactionType || ![reactionType isKindOfClass:[NSString class]] || reactionType.length == 0) {
        return nil;
    }
    if ([reactionType hasPrefix:@"-"] && reactionType.length > 1) {
        if (outIsRemoved) {
            *outIsRemoved = YES;
        }
        return [reactionType substringFromIndex:1];
    }
    return reactionType;
}

static long long swizzled_parseReactionType(id self, SEL _cmd, NSString *reactionType) {
    if (isClassicReaction(reactionType)) {
        pthread_mutex_lock(&g_emojiMutex);
        g_pendingEmoji = nil;
        pthread_mutex_unlock(&g_emojiMutex);
        return ((long long(*)(id, SEL, NSString *))g_origParseReactionType)(self, _cmd, reactionType);
    }

    BOOL isRemoved = NO;
    NSString *emoji = extractEmojiAndMode(reactionType, &isRemoved);
    if (emoji.length > 0) {
        pthread_mutex_lock(&g_emojiMutex);
        g_pendingEmoji = [emoji copy];
        pthread_mutex_unlock(&g_emojiMutex);
        return isRemoved ? kIMAssociatedMessageTypeEmojiRemoved : kIMAssociatedMessageTypeEmojiAdded;
    }

    return ((long long(*)(id, SEL, NSString *))g_origParseReactionType)(self, _cmd, reactionType);
}

static NSString *swizzled_reactionToVerb(id self, SEL _cmd, NSString *reactionType) {
    if (isClassicReaction(reactionType)) {
        return ((NSString *(*)(id, SEL, NSString *))g_origReactionToVerb)(self, _cmd, reactionType);
    }

    BOOL isRemoved = NO;
    NSString *emoji = extractEmojiAndMode(reactionType, &isRemoved);
    if (emoji.length > 0) {
        pthread_mutex_lock(&g_emojiMutex);
        g_pendingEmoji = [emoji copy];
        pthread_mutex_unlock(&g_emojiMutex);
        if (isRemoved) {
            return [NSString stringWithFormat:@"Removed %@ from ", emoji];
        }
        return [NSString stringWithFormat:@"Reacted %@ to ", emoji];
    }

    return ((NSString *(*)(id, SEL, NSString *))g_origReactionToVerb)(self, _cmd, reactionType);
}

static NSString *extractEmojiFromReactionText(NSAttributedString *text, long long associatedMessageType) {
    if (!text || ![text respondsToSelector:@selector(string)]) {
        return nil;
    }
    NSString *raw = [text string];
    if (!raw || raw.length == 0) {
        return nil;
    }
    if (associatedMessageType == kIMAssociatedMessageTypeEmojiAdded && [raw hasPrefix:@"Reacted "]) {
        NSRange toRange = [raw rangeOfString:@" to "];
        if (toRange.location != NSNotFound && toRange.location > 8) {
            return [raw substringWithRange:NSMakeRange(8, toRange.location - 8)];
        }
    } else if (associatedMessageType == kIMAssociatedMessageTypeEmojiRemoved && [raw hasPrefix:@"Removed "]) {
        NSRange fromRange = [raw rangeOfString:@" from "];
        if (fromRange.location != NSNotFound && fromRange.location > 8) {
            return [raw substringWithRange:NSMakeRange(8, fromRange.location - 8)];
        }
    }
    return nil;
}

static id swizzled_initWithSender(
    id self,
    SEL _cmd,
    id sender,
    id time,
    NSAttributedString *text,
    id messageSubject,
    id fileTransferGUIDs,
    unsigned long long flags,
    id error,
    id guid,
    id subject,
    id associatedMessageGUID,
    long long associatedMessageType,
    NSRange associatedMessageRange,
    id messageSummaryInfo
) {
    id msg = ((id(*)(
        id, SEL, id, id, id, id, id, unsigned long long, id, id, id, id, long long, NSRange, id
    ))g_origInitWithSender)(
        self,
        _cmd,
        sender,
        time,
        text,
        messageSubject,
        fileTransferGUIDs,
        flags,
        error,
        guid,
        subject,
        associatedMessageGUID,
        associatedMessageType,
        associatedMessageRange,
        messageSummaryInfo
    );

    if (msg && (associatedMessageType == kIMAssociatedMessageTypeEmojiAdded ||
                associatedMessageType == kIMAssociatedMessageTypeEmojiRemoved)) {
        NSString *emoji = extractEmojiFromReactionText(text, associatedMessageType);
        if (!emoji || emoji.length == 0) {
            pthread_mutex_lock(&g_emojiMutex);
            emoji = [g_pendingEmoji copy];
            pthread_mutex_unlock(&g_emojiMutex);
        }
        SEL setEmojiSel = sel_registerName("_associatedMessageEmoji:");
        if (emoji.length > 0 && [msg respondsToSelector:setEmojiSel]) {
            ((void(*)(id, SEL, id))objc_msgSend)(msg, setEmojiSel, emoji);
            os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
            os_log(log, "BLUEBUBBLESHELPER: Attached custom associatedMessageEmoji '%{public}@' (type=%lld)",
                   emoji, associatedMessageType);
        }
    }

    return msg;
}

// ---------------------------------------------------------------------------
// Shared Helpers for NetworkController & BlueBubblesHelper
// ---------------------------------------------------------------------------

static void sendTransactionResponse(NSString *transactionId, NSString *identifier, NSString *errorMsg) {
    if (!transactionId || ![transactionId isKindOfClass:[NSString class]] || transactionId.length == 0) {
        return;
    }
    Class netCls = objc_getClass("NetworkController");
    if (!netCls) {
        return;
    }
    id net = ((id(*)(id, SEL))objc_msgSend)(netCls, sel_registerName("sharedInstance"));
    if (!net) {
        return;
    }
    NSMutableDictionary *resp = [NSMutableDictionary dictionary];
    resp[@"transactionId"] = transactionId;
    if (errorMsg.length > 0) {
        resp[@"error"] = errorMsg;
    } else if (identifier.length > 0) {
        resp[@"identifier"] = identifier;
    }
    ((void(*)(id, SEL, id))objc_msgSend)(net, sel_registerName("sendMessage:"), resp);
}

static id getIMChat(NSString *chatGuid, NSString *transactionId) {
    Class helperCls = objc_getClass("BlueBubblesHelper");
    if (!helperCls) {
        return nil;
    }
    SEL getChatSel = sel_registerName("getChat::");
    if (![helperCls respondsToSelector:getChatSel]) {
        return nil;
    }
    return ((id(*)(id, SEL, id, id))objc_msgSend)(helperCls, getChatSel, chatGuid, transactionId);
}

static void getIMMessageItem(id chat, NSString *guid, void (^completion)(id item, id msgItem)) {
    Class helperCls = objc_getClass("BlueBubblesHelper");
    SEL sel = sel_registerName("getMessageItem::completionBlock:");
    if (!chat || !guid || guid.length == 0 || !helperCls || ![helperCls respondsToSelector:sel]) {
        if (completion) {
            completion(nil, nil);
        }
        return;
    }
    void (^block)(id) = ^(id item) {
        id msgItem = nil;
        SEL imItemSel = sel_registerName("_imMessageItem");
        if (item && [item respondsToSelector:imItemSel]) {
            msgItem = ((id(*)(id, SEL))objc_msgSend)(item, imItemSel);
        } else {
            msgItem = item;
        }
        if (completion) {
            completion(item, msgItem);
        }
    };
    ((void(*)(id, SEL, id, id, id))objc_msgSend)(helperCls, sel, chat, guid, block);
}

static NSString *extractChatCreatorHandle(id chat) {
    if (!chat) {
        return @"";
    }
    SEL accountSel = sel_registerName("account");
    if ([chat respondsToSelector:accountSel]) {
        id account = ((id(*)(id, SEL))objc_msgSend)(chat, accountSel);
        SEL loginHandleSel = sel_registerName("loginIMHandle");
        if (account && [account respondsToSelector:loginHandleSel]) {
            id loginHandle = ((id(*)(id, SEL))objc_msgSend)(account, loginHandleSel);
            SEL idSel = sel_registerName("ID");
            if (loginHandle && [loginHandle respondsToSelector:idSel]) {
                NSString *handleId = ((id(*)(id, SEL))objc_msgSend)(loginHandle, idSel);
                if ([handleId isKindOfClass:[NSString class]] && handleId.length > 0) {
                    return handleId;
                }
            }
        }
    }
    return @"";
}

// ---------------------------------------------------------------------------
// 2. Text Formatting & Animated Text Effects + 3. Send Later
// ---------------------------------------------------------------------------

static long long resolveTextEffectCode(id rawEffect) {
    if (!rawEffect) {
        return 0;
    }
    if ([rawEffect isKindOfClass:[NSNumber class]]) {
        long long code = [rawEffect longLongValue];
        return (code >= 1 && code <= 12) ? code : 0;
    }
    if (![rawEffect isKindOfClass:[NSString class]]) {
        return 0;
    }
    static NSDictionary<NSString *, NSNumber *> *effectMap = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        effectMap = @{
            @"ripple": @1,
            @"scaleripple": @1,
            @"scale_ripple": @1,
            @"stretch": @2,
            @"squish": @3,
            @"bounce": @4,
            @"big": @5,
            @"bloom": @6,
            @"somersault": @7,
            @"nod": @8,
            @"shakevertical": @8,
            @"shake_vertical": @8,
            @"shake": @9,
            @"shakehorizontal": @9,
            @"shake_horizontal": @9,
            @"jitter": @10,
            @"small": @11,
            @"explode": @12,
        };
    });
    NSString *key = [[rawEffect lowercaseString] stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    return [effectMap[key] longLongValue];
}

static void applyFormattingToRange(
    NSMutableAttributedString *attrText,
    NSRange range,
    NSArray *styles,
    NSDictionary *flagsDict,
    id textEffect
) {
    if (!attrText || attrText.length == 0) {
        return;
    }
    if (range.location >= attrText.length) {
        return;
    }
    if (range.location + range.length > attrText.length) {
        range.length = attrText.length - range.location;
    }
    if (range.length == 0) {
        return;
    }

    NSMutableSet<NSString *> *styleSet = [NSMutableSet set];
    if ([styles isKindOfClass:[NSArray class]]) {
        for (id item in styles) {
            if ([item isKindOfClass:[NSString class]]) {
                [styleSet addObject:[item lowercaseString]];
            }
        }
    }
    if ([flagsDict isKindOfClass:[NSDictionary class]]) {
        if ([flagsDict[@"bold"] boolValue]) [styleSet addObject:@"bold"];
        if ([flagsDict[@"italic"] boolValue]) [styleSet addObject:@"italic"];
        if ([flagsDict[@"underline"] boolValue]) [styleSet addObject:@"underline"];
        if ([flagsDict[@"strikethrough"] boolValue] || [flagsDict[@"strike"] boolValue]) {
            [styleSet addObject:@"strikethrough"];
        }
    }

    if ([styleSet containsObject:@"bold"]) {
        [attrText addAttribute:@"__kIMTextBoldAttributeName" value:@1 range:range];
    }
    if ([styleSet containsObject:@"italic"]) {
        [attrText addAttribute:@"__kIMTextItalicAttributeName" value:@1 range:range];
    }
    if ([styleSet containsObject:@"underline"]) {
        [attrText addAttribute:@"__kIMTextUnderlineAttributeName" value:@1 range:range];
    }
    if ([styleSet containsObject:@"strikethrough"] || [styleSet containsObject:@"strike"]) {
        [attrText addAttribute:@"__kIMTextStrikethroughAttributeName" value:@1 range:range];
    }

    long long effectCode = resolveTextEffectCode(textEffect);
    if (effectCode >= 1 && effectCode <= 12) {
        [attrText addAttribute:@"__kIMTextEffectAttributeName" value:@(effectCode) range:range];
    }
}

static void handleFormattedOrScheduledMessage(
    id chat,
    NSString *transactionId,
    NSDictionary *data,
    NSDictionary *spec
) {
    NSString *rawText = [data[@"message"] isKindOfClass:[NSString class]] ? data[@"message"] : @"";
    NSMutableAttributedString *attrText = [[NSMutableAttributedString alloc]
        initWithString:rawText
            attributes:@{@"__kIMMessagePartAttributeName": @0}];

    NSRange fullRange = NSMakeRange(0, attrText.length);
    applyFormattingToRange(attrText, fullRange, spec[@"styles"], spec, spec[@"text_effect"]);

    NSArray *ranges = [spec[@"ranges"] isKindOfClass:[NSArray class]] ? spec[@"ranges"] : nil;
    for (id entry in ranges) {
        if (![entry isKindOfClass:[NSDictionary class]]) {
            continue;
        }
        NSDictionary *rDict = (NSDictionary *)entry;
        NSInteger start = [rDict[@"start"] integerValue];
        NSInteger length = [rDict[@"length"] integerValue];
        if (start < 0 || length <= 0) {
            continue;
        }
        applyFormattingToRange(
            attrText,
            NSMakeRange((NSUInteger)start, (NSUInteger)length),
            rDict[@"styles"],
            rDict,
            rDict[@"text_effect"] ?: rDict[@"effect"]
        );
    }

    NSString *rawSubject = [data[@"subject"] isKindOfClass:[NSString class]] ? data[@"subject"] : nil;
    NSAttributedString *subjectAttr = (rawSubject.length > 0)
        ? [[NSAttributedString alloc] initWithString:rawSubject]
        : nil;

    NSString *bubbleEffect = [spec[@"bubble_effect"] isKindOfClass:[NSString class]]
        ? spec[@"bubble_effect"]
        : nil;
    if (bubbleEffect.length == 0) {
        bubbleEffect = nil;
    }

    NSString *selectedGuid = [data[@"selectedMessageGuid"] isKindOfClass:[NSString class]]
        ? data[@"selectedMessageGuid"]
        : nil;
    NSInteger partIndex = [data[@"partIndex"] respondsToSelector:@selector(integerValue)]
        ? [data[@"partIndex"] integerValue]
        : 0;
    NSString *threadId = (selectedGuid.length > 0)
        ? [NSString stringWithFormat:@"p:%ld/%@", (long)partIndex, selectedGuid]
        : nil;

    double scheduledTs = [spec[@"scheduled_at"] respondsToSelector:@selector(doubleValue)]
        ? [spec[@"scheduled_at"] doubleValue]
        : 0.0;

    Class imMessageCls = objc_getClass("IMMessage");
    id msg = nil;

    if (scheduledTs > 0.0) {
        NSDate *scheduledDate = [NSDate dateWithTimeIntervalSince1970:scheduledTs];
        SEL schedFactorySel = sel_registerName(
            "instantMessageWithText:messageSubject:flags:threadIdentifier:associatedMessageGUID:scheduledDate:"
        );
        if ([imMessageCls respondsToSelector:schedFactorySel]) {
            msg = ((id(*)(id, SEL, id, id, unsigned long long, id, id, id))objc_msgSend)(
                imMessageCls,
                schedFactorySel,
                attrText,
                subjectAttr,
                0x100005ULL,
                threadId,
                nil,
                scheduledDate
            );
        }
        if (msg) {
            SEL setSchedTypeSel = sel_registerName("setScheduleType:");
            SEL setSchedStateSel = sel_registerName("setScheduleState:");
            if ([msg respondsToSelector:setSchedTypeSel]) {
                ((void(*)(id, SEL, unsigned long long))objc_msgSend)(msg, setSchedTypeSel, 2ULL);
            }
            if ([msg respondsToSelector:setSchedStateSel]) {
                ((void(*)(id, SEL, unsigned long long))objc_msgSend)(msg, setSchedStateSel, 1ULL);
            }
            if (bubbleEffect && [msg respondsToSelector:sel_registerName("setExpressiveSendStyleID:")]) {
                ((void(*)(id, SEL, id))objc_msgSend)(msg, sel_registerName("setExpressiveSendStyleID:"), bubbleEffect);
            }
        }
    } else {
        SEL initSel = sel_registerName(
            "initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:"
            "balloonBundleID:payloadData:expressiveSendStyleID:"
        );
        id alloced = [imMessageCls alloc];
        if ([alloced respondsToSelector:initSel]) {
            msg = ((id(*)(id, SEL, id, id, id, id, id, unsigned long long, id, id, id, id, id, id))objc_msgSend)(
                alloced,
                initSel,
                nil,
                [NSDate date],
                attrText,
                subjectAttr,
                nil,
                5ULL,
                nil,
                nil,
                nil,
                nil,
                nil,
                bubbleEffect
            );
        }
        if (msg && threadId.length > 0 && [msg respondsToSelector:sel_registerName("setThreadIdentifier:")]) {
            ((void(*)(id, SEL, id))objc_msgSend)(msg, sel_registerName("setThreadIdentifier:"), threadId);
        }
    }

    if (!msg) {
        sendTransactionResponse(transactionId, nil, @"Failed to construct IMMessage");
        return;
    }

    ((void(*)(id, SEL, id))objc_msgSend)(chat, sel_registerName("sendMessage:"), msg);
    NSString *msgGuid = [msg respondsToSelector:sel_registerName("guid")]
        ? ((id(*)(id, SEL))objc_msgSend)(msg, sel_registerName("guid"))
        : @"";
    os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
    os_log(log, "BLUEBUBBLESHELPER: Sent custom message guid=%{public}@ (scheduled=%.0f)", msgGuid, scheduledTs);
    sendTransactionResponse(transactionId, msgGuid, nil);
}

// ---------------------------------------------------------------------------
// 4. Native iMessage Polls (create / vote / add_option)
// ---------------------------------------------------------------------------

static NSData *archivePollPayloadDict(NSDictionary *pollJson, NSUUID *sessionUUID, NSString *summaryText) {
    NSError *jsonErr = nil;
    NSData *jsonData = [NSJSONSerialization dataWithJSONObject:pollJson
                                                       options:NSJSONWritingWithoutEscapingSlashes
                                                         error:&jsonErr];
    if (!jsonData) {
        return nil;
    }
    NSString *b64 = [jsonData base64EncodedStringWithOptions:0];
    NSURL *dataUrl = [NSURL URLWithString:[NSString stringWithFormat:@"data:,%@", b64]];
    if (!dataUrl) {
        return nil;
    }
    NSDictionary *payloadDict = @{
        @"URL": dataUrl,
        @"sessionIdentifier": sessionUUID ?: [NSUUID UUID],
        @"ldtext": (summaryText.length > 0) ? summaryText : @"Poll"
    };
    return [NSKeyedArchiver archivedDataWithRootObject:payloadDict requiringSecureCoding:YES error:nil];
}

static NSDictionary *decodeExistingPollPayload(NSData *payloadData, NSUUID **outSessionUUID) {
    if (!payloadData || ![payloadData isKindOfClass:[NSData class]] || payloadData.length == 0) {
        return nil;
    }
    NSSet *allowedClasses = [NSSet setWithArray:@[
        [NSDictionary class], [NSArray class], [NSString class],
        [NSNumber class], [NSURL class], [NSUUID class], [NSData class]
    ]];
    NSDictionary *unarchived = [NSKeyedUnarchiver unarchivedObjectOfClasses:allowedClasses
                                                                   fromData:payloadData
                                                                      error:nil];
    if (![unarchived isKindOfClass:[NSDictionary class]]) {
        return nil;
    }
    if (outSessionUUID && [unarchived[@"sessionIdentifier"] isKindOfClass:[NSUUID class]]) {
        *outSessionUUID = unarchived[@"sessionIdentifier"];
    }
    id urlObj = unarchived[@"URL"];
    NSString *urlStr = [urlObj isKindOfClass:[NSURL class]]
        ? [(NSURL *)urlObj absoluteString]
        : ([urlObj isKindOfClass:[NSString class]] ? (NSString *)urlObj : nil);
    if (!urlStr || ![urlStr hasPrefix:@"data:,"]) {
        return nil;
    }
    NSString *b64 = [urlStr substringFromIndex:6];
    NSData *jsonData = [[NSData alloc] initWithBase64EncodedString:b64 options:NSDataBase64DecodingIgnoreUnknownCharacters];
    if (!jsonData) {
        return nil;
    }
    id parsed = [NSJSONSerialization JSONObjectWithData:jsonData options:0 error:nil];
    return [parsed isKindOfClass:[NSDictionary class]] ? (NSDictionary *)parsed : nil;
}

static void handlePollAction(
    id chat,
    NSString *transactionId,
    NSDictionary *data,
    NSDictionary *spec
) {
    NSString *mode = [spec[@"mode"] isKindOfClass:[NSString class]] ? spec[@"mode"] : @"poll_create";
    NSString *creatorHandle = extractChatCreatorHandle(chat);

    if ([mode isEqualToString:@"poll_create"]) {
        NSString *title = [spec[@"title"] isKindOfClass:[NSString class]] ? spec[@"title"] : @"";
        NSArray *rawOptions = [spec[@"options"] isKindOfClass:[NSArray class]] ? spec[@"options"] : @[];
        NSMutableArray *orderedOptions = [NSMutableArray array];

        for (id rawOpt in rawOptions) {
            NSString *optText = nil;
            NSString *optId = nil;
            BOOL canBeEdited = NO;
            if ([rawOpt isKindOfClass:[NSString class]]) {
                optText = (NSString *)rawOpt;
            } else if ([rawOpt isKindOfClass:[NSDictionary class]]) {
                NSDictionary *optDict = (NSDictionary *)rawOpt;
                optText = [optDict[@"text"] isKindOfClass:[NSString class]] ? optDict[@"text"] : nil;
                optId = [optDict[@"id"] isKindOfClass:[NSString class]] ? optDict[@"id"] :
                        ([optDict[@"optionIdentifier"] isKindOfClass:[NSString class]] ? optDict[@"optionIdentifier"] : nil);
                canBeEdited = [optDict[@"can_be_edited"] boolValue] || [optDict[@"canBeEdited"] boolValue];
            }
            if (!optText || optText.length == 0) {
                continue;
            }
            if (!optId || optId.length == 0) {
                optId = [[[NSUUID UUID] UUIDString] uppercaseString];
            }
            [orderedOptions addObject:@{
                @"optionIdentifier": optId,
                @"creatorHandle": creatorHandle,
                @"text": optText,
                @"canBeEdited": @(canBeEdited)
            }];
        }

        if (orderedOptions.count < 2) {
            sendTransactionResponse(transactionId, nil, @"A poll requires at least 2 options");
            return;
        }

        NSDictionary *pollJson = @{
            @"version": @1,
            @"item": @{
                @"title": title ?: @"",
                @"creatorHandle": creatorHandle,
                @"orderedPollOptions": orderedOptions
            }
        };

        NSUUID *sessionUUID = [NSUUID UUID];
        NSString *summaryText = (title.length > 0) ? title : @"Poll";
        NSData *archivedPayload = archivePollPayloadDict(pollJson, sessionUUID, summaryText);
        if (!archivedPayload) {
            sendTransactionResponse(transactionId, nil, @"Failed to archive poll payload");
            return;
        }

        NSAttributedString *attrText = [[NSAttributedString alloc]
            initWithString:@"\ufffc"
                attributes:@{@"__kIMMessagePartAttributeName": @0}];
        NSString *msgGuid = [sessionUUID UUIDString];

        Class imMessageCls = objc_getClass("IMMessage");
        SEL initSel = sel_registerName(
            "initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:"
            "balloonBundleID:payloadData:expressiveSendStyleID:"
        );
        id msg = ((id(*)(id, SEL, id, id, id, id, id, unsigned long long, id, id, id, id, id, id))objc_msgSend)(
            [imMessageCls alloc],
            initSel,
            nil,
            [NSDate date],
            attrText,
            nil,
            nil,
            0x100005ULL,
            nil,
            msgGuid,
            nil,
            kPollBalloonBundleID,
            archivedPayload,
            nil
        );

        NSString *selectedGuid = [data[@"selectedMessageGuid"] isKindOfClass:[NSString class]]
            ? data[@"selectedMessageGuid"]
            : nil;
        if (msg && selectedGuid.length > 0 && [msg respondsToSelector:sel_registerName("setThreadIdentifier:")]) {
            NSString *threadId = [NSString stringWithFormat:@"p:0/%@", selectedGuid];
            ((void(*)(id, SEL, id))objc_msgSend)(msg, sel_registerName("setThreadIdentifier:"), threadId);
        }

        if (!msg) {
            sendTransactionResponse(transactionId, nil, @"Failed to create poll IMMessage");
            return;
        }

        ((void(*)(id, SEL, id))objc_msgSend)(chat, sel_registerName("sendMessage:"), msg);
        NSString *outGuid = [msg respondsToSelector:sel_registerName("guid")]
            ? ((id(*)(id, SEL))objc_msgSend)(msg, sel_registerName("guid"))
            : msgGuid;
        os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
        os_log(log, "BLUEBUBBLESHELPER: Created poll guid=%{public}@ options=%lu",
               outGuid, (unsigned long)orderedOptions.count);
        sendTransactionResponse(transactionId, outGuid, nil);
        return;
    }

    // poll_vote or poll_add_option on an existing poll message
    NSString *pollGuid = [spec[@"poll_guid"] isKindOfClass:[NSString class]] ? spec[@"poll_guid"] : nil;
    if (!pollGuid || pollGuid.length == 0) {
        pollGuid = [data[@"selectedMessageGuid"] isKindOfClass:[NSString class]] ? data[@"selectedMessageGuid"] : nil;
    }
    if (!pollGuid || pollGuid.length == 0) {
        sendTransactionResponse(transactionId, nil, @"poll_guid is required for poll updates");
        return;
    }

    getIMMessageItem(chat, pollGuid, ^(id item, id msgItem) {
        NSData *existingPayloadData = nil;
        SEL pdSel = sel_registerName("payloadData");
        if (msgItem && [msgItem respondsToSelector:pdSel]) {
            existingPayloadData = ((id(*)(id, SEL))objc_msgSend)(msgItem, pdSel);
        } else if (item && [item respondsToSelector:pdSel]) {
            existingPayloadData = ((id(*)(id, SEL))objc_msgSend)(item, pdSel);
        }

        NSUUID *sessionUUID = [[NSUUID alloc] initWithUUIDString:pollGuid] ?: [NSUUID UUID];
        NSDictionary *existingJson = decodeExistingPollPayload(existingPayloadData, &sessionUUID);
        NSDictionary *existingItem = [existingJson[@"item"] isKindOfClass:[NSDictionary class]]
            ? existingJson[@"item"]
            : @{};
        NSArray *existingOptions = [existingItem[@"orderedPollOptions"] isKindOfClass:[NSArray class]]
            ? existingItem[@"orderedPollOptions"]
            : @[];
        NSString *pollTitle = [existingItem[@"title"] isKindOfClass:[NSString class]]
            ? existingItem[@"title"]
            : @"Poll";

        NSDictionary *updateJson = nil;
        NSString *summaryText = pollTitle.length > 0 ? pollTitle : @"Poll";

        if ([mode isEqualToString:@"poll_vote"]) {
            NSMutableArray<NSString *> *resolvedOptionIds = [NSMutableArray array];
            NSArray *requestedTargets = nil;
            if ([spec[@"options"] isKindOfClass:[NSArray class]]) {
                requestedTargets = spec[@"options"];
            } else if (spec[@"option"]) {
                requestedTargets = @[spec[@"option"]];
            } else if (spec[@"option_id"]) {
                requestedTargets = @[spec[@"option_id"]];
            } else if (spec[@"option_index"]) {
                requestedTargets = @[spec[@"option_index"]];
            } else if (spec[@"option_text"]) {
                requestedTargets = @[spec[@"option_text"]];
            }

            for (id target in requestedTargets) {
                NSString *matchedId = nil;
                if ([target isKindOfClass:[NSNumber class]]) {
                    NSInteger idx1 = [target integerValue];
                    NSInteger idx0 = (idx1 >= 1) ? (idx1 - 1) : idx1;
                    if (idx0 >= 0 && (NSUInteger)idx0 < existingOptions.count) {
                        NSDictionary *opt = existingOptions[(NSUInteger)idx0];
                        matchedId = opt[@"optionIdentifier"];
                    }
                } else if ([target isKindOfClass:[NSString class]]) {
                    NSString *targetStr = [(NSString *)target stringByTrimmingCharactersInSet:
                                           [NSCharacterSet whitespaceAndNewlineCharacterSet]];
                    for (NSDictionary *opt in existingOptions) {
                        if (![opt isKindOfClass:[NSDictionary class]]) continue;
                        NSString *oid = opt[@"optionIdentifier"];
                        NSString *otext = opt[@"text"];
                        if ((oid && [oid caseInsensitiveCompare:targetStr] == NSOrderedSame) ||
                            (otext && [otext caseInsensitiveCompare:targetStr] == NSOrderedSame)) {
                            matchedId = oid;
                            break;
                        }
                    }
                    if (!matchedId) {
                        NSInteger parsedIdx = [targetStr integerValue];
                        if (parsedIdx >= 1 && (NSUInteger)(parsedIdx - 1) < existingOptions.count) {
                            matchedId = existingOptions[(NSUInteger)(parsedIdx - 1)][@"optionIdentifier"];
                        } else if ([[NSUUID alloc] initWithUUIDString:targetStr] != nil) {
                            matchedId = [targetStr uppercaseString];
                        }
                    }
                }
                if (matchedId.length > 0 && ![resolvedOptionIds containsObject:matchedId]) {
                    [resolvedOptionIds addObject:matchedId];
                }
            }

            if (resolvedOptionIds.count == 0) {
                sendTransactionResponse(transactionId, nil, @"Could not match requested poll option");
                return;
            }

            double serverVoteTime = [[NSDate date] timeIntervalSinceReferenceDate];
            NSMutableArray *votes = [NSMutableArray array];
            for (NSString *oid in resolvedOptionIds) {
                [votes addObject:@{
                    @"voteOptionIdentifier": oid,
                    @"participantHandle": creatorHandle,
                    @"serverVoteTime": @(serverVoteTime)
                }];
            }
            updateJson = @{
                @"version": @1,
                @"item": @{
                    @"votes": votes
                }
            };
        } else if ([mode isEqualToString:@"poll_add_option"]) {
            NSString *newText = [spec[@"option_text"] isKindOfClass:[NSString class]]
                ? spec[@"option_text"]
                : ([spec[@"option"] isKindOfClass:[NSString class]] ? spec[@"option"] : nil);
            if (!newText || newText.length == 0) {
                sendTransactionResponse(transactionId, nil, @"option_text is required for poll_add_option");
                return;
            }
            NSMutableArray *updatedOptions = [NSMutableArray arrayWithArray:existingOptions];
            NSString *newOptId = [[[NSUUID UUID] UUIDString] uppercaseString];
            [updatedOptions addObject:@{
                @"optionIdentifier": newOptId,
                @"creatorHandle": creatorHandle,
                @"text": newText,
                @"canBeEdited": @NO
            }];
            NSString *origCreator = [existingItem[@"creatorHandle"] isKindOfClass:[NSString class]]
                ? existingItem[@"creatorHandle"]
                : creatorHandle;
            updateJson = @{
                @"version": @1,
                @"item": @{
                    @"title": pollTitle ?: @"",
                    @"creatorHandle": origCreator ?: @"",
                    @"orderedPollOptions": updatedOptions
                }
            };
        } else {
            sendTransactionResponse(transactionId, nil, @"Unsupported poll mode");
            return;
        }

        NSData *archivedUpdate = archivePollPayloadDict(updateJson, sessionUUID, summaryText);
        if (!archivedUpdate) {
            sendTransactionResponse(transactionId, nil, @"Failed to archive poll update payload");
            return;
        }

        NSAttributedString *attrText = [[NSAttributedString alloc]
            initWithString:@"\ufffc"
                attributes:@{@"__kIMMessagePartAttributeName": @0}];

        Class imMessageCls = objc_getClass("IMMessage");
        SEL breadcrumbSel = sel_registerName(
            "breadcrumbMessageWithText:associatedMessageGUID:balloonBundleID:fileTransferGUIDs:payloadData:threadIdentifier:"
        );
        id msg = nil;
        if ([imMessageCls respondsToSelector:breadcrumbSel]) {
            msg = ((id(*)(id, SEL, id, id, id, id, id, id))objc_msgSend)(
                imMessageCls,
                breadcrumbSel,
                attrText,
                pollGuid,
                kPollBalloonBundleID,
                nil,
                archivedUpdate,
                nil
            );
        }
        if (!msg) {
            sendTransactionResponse(transactionId, nil, @"Failed to construct poll update message");
            return;
        }

        ((void(*)(id, SEL, id))objc_msgSend)(chat, sel_registerName("sendMessage:"), msg);
        NSString *outGuid = [msg respondsToSelector:sel_registerName("guid")]
            ? ((id(*)(id, SEL))objc_msgSend)(msg, sel_registerName("guid"))
            : @"";
        os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
        os_log(log, "BLUEBUBBLESHELPER: Sent poll update (%@) guid=%{public}@ for poll=%{public}@",
               mode, outGuid, pollGuid);
        sendTransactionResponse(transactionId, outGuid, nil);
    });
}

// ---------------------------------------------------------------------------
// 5. Sticker Tapbacks (2007 / 3007) & Freeform Sticker Placement (1000 / standalone)
// ---------------------------------------------------------------------------

static void handleStickerAttachment(
    id chat,
    NSString *transactionId,
    NSDictionary *data,
    NSDictionary *spec
) {
    NSString *filePath = [data[@"filePath"] isKindOfClass:[NSString class]] ? data[@"filePath"] : nil;
    if (!filePath || filePath.length == 0) {
        sendTransactionResponse(transactionId, nil, @"Missing sticker filePath");
        return;
    }
    NSString *filename = [filePath lastPathComponent];

    Class helperCls = objc_getClass("BlueBubblesHelper");
    SEL prepSel = sel_registerName("prepareFileTransferForAttachment:filename:");
    if (!helperCls || ![helperCls respondsToSelector:prepSel]) {
        sendTransactionResponse(transactionId, nil, @"prepareFileTransferForAttachment unavailable");
        return;
    }

    NSURL *fileUrl = [NSURL fileURLWithPath:filePath];
    NSString *transferGuid = ((id(*)(id, SEL, id, id))objc_msgSend)(helperCls, prepSel, fileUrl, filename);
    if (!transferGuid || transferGuid.length == 0) {
        sendTransactionResponse(transactionId, nil, @"Failed to create sticker file transfer");
        return;
    }

    Class ftcCls = objc_getClass("IMFileTransferCenter");
    id ftc = ftcCls ? ((id(*)(id, SEL))objc_msgSend)(ftcCls, sel_registerName("sharedInstance")) : nil;
    id transfer = (ftc && [ftc respondsToSelector:sel_registerName("transferForGUID:")])
        ? ((id(*)(id, SEL, id))objc_msgSend)(ftc, sel_registerName("transferForGUID:"), transferGuid)
        : nil;

    if (transfer) {
        if ([transfer respondsToSelector:sel_registerName("setIsSticker:")]) {
            ((void(*)(id, SEL, BOOL))objc_msgSend)(transfer, sel_registerName("setIsSticker:"), YES);
        }

        Class stickerCls = objc_getClass("IMSticker");
        SEL uiSel = sel_registerName(
            "userInfoDictionaryWithLayoutIntent:parentPreviewWidth:xScalar:yScalar:scale:rotation:initialFrameIndex:externalURI:"
        );
        if (stickerCls && [stickerCls respondsToSelector:uiSel]) {
            unsigned long long layoutIntent = [spec[@"layout_intent"] respondsToSelector:@selector(unsignedLongLongValue)]
                ? [spec[@"layout_intent"] unsignedLongLongValue]
                : 0ULL;
            double previewWidth = [spec[@"parent_preview_width"] respondsToSelector:@selector(doubleValue)]
                ? [spec[@"parent_preview_width"] doubleValue]
                : 300.0;
            double xScalar = [spec[@"x_scalar"] respondsToSelector:@selector(doubleValue)]
                ? [spec[@"x_scalar"] doubleValue]
                : 0.0;
            double yScalar = [spec[@"y_scalar"] respondsToSelector:@selector(doubleValue)]
                ? [spec[@"y_scalar"] doubleValue]
                : 0.0;
            double scale = [spec[@"scale"] respondsToSelector:@selector(doubleValue)]
                ? [spec[@"scale"] doubleValue]
                : 1.0;
            double rotation = [spec[@"rotation"] respondsToSelector:@selector(doubleValue)]
                ? [spec[@"rotation"] doubleValue]
                : 0.0;
            NSDictionary *stickerInfo = ((id(*)(
                id, SEL, unsigned long long, double, double, double, double, double, unsigned long long, id
            ))objc_msgSend)(
                stickerCls, uiSel, layoutIntent, previewWidth, xScalar, yScalar, scale, rotation, 0ULL, nil
            );
            if (stickerInfo && [transfer respondsToSelector:sel_registerName("setStickerUserInfo:")]) {
                ((void(*)(id, SEL, id))objc_msgSend)(transfer, sel_registerName("setStickerUserInfo:"), stickerInfo);
            }
        }
        if (ftc && [ftc respondsToSelector:sel_registerName("registerTransferWithDaemon:")]) {
            ((void(*)(id, SEL, id))objc_msgSend)(ftc, sel_registerName("registerTransferWithDaemon:"), transferGuid);
        }
    }

    NSString *mode = [spec[@"mode"] isKindOfClass:[NSString class]] ? spec[@"mode"] : @"sticker";
    BOOL isTapback = [mode isEqualToString:@"sticker_tapback"] || [spec[@"is_tapback"] boolValue];
    BOOL isRemoved = [spec[@"remove"] boolValue] || [mode isEqualToString:@"sticker_tapback_remove"];
    NSString *selectedGuid = [data[@"selectedMessageGuid"] isKindOfClass:[NSString class]]
        ? data[@"selectedMessageGuid"]
        : ([spec[@"selected_message_guid"] isKindOfClass:[NSString class]] ? spec[@"selected_message_guid"] : nil);
    NSInteger partIndex = [data[@"partIndex"] respondsToSelector:@selector(integerValue)]
        ? [data[@"partIndex"] integerValue]
        : 0;

    if (selectedGuid.length > 0) {
        getIMMessageItem(chat, selectedGuid, ^(id item, id msgItem) {
            NSString *summaryStr = @"Message";
            if (msgItem && [msgItem respondsToSelector:sel_registerName("body")]) {
                NSAttributedString *body = ((id(*)(id, SEL))objc_msgSend)(msgItem, sel_registerName("body"));
                if (body && [body respondsToSelector:@selector(string)] && [body string].length > 0) {
                    summaryStr = [body string];
                }
            } else if (item && [item respondsToSelector:sel_registerName("text")]) {
                NSAttributedString *body = ((id(*)(id, SEL))objc_msgSend)(item, sel_registerName("text"));
                if (body && [body respondsToSelector:@selector(string)] && [body string].length > 0) {
                    summaryStr = [body string];
                }
            }
            NSRange partRange = NSMakeRange(0, summaryStr.length > 0 ? summaryStr.length : 1);
            NSDictionary *msi = @{@"amc": @1, @"ams": summaryStr};

            id msg = nil;
            if (isTapback) {
                Class stCls = objc_getClass("IMStickerTapback");
                SEL initTbSel = sel_registerName("initWithTransferGUID:isRemoved:");
                SEL msgForChatSel = sel_registerName(
                    "messageForChat:messageGUID:messagePartRange:messageSummaryInfo:threadIdentifier:"
                );
                if (stCls && [stCls instancesRespondToSelector:initTbSel] &&
                    [stCls instancesRespondToSelector:msgForChatSel]) {
                    id tb = ((id(*)(id, SEL, id, BOOL))objc_msgSend)([stCls alloc], initTbSel, transferGuid, isRemoved);
                    if (tb) {
                        msg = ((id(*)(id, SEL, id, id, NSRange, id, id))objc_msgSend)(
                            tb, msgForChatSel, chat, selectedGuid, partRange, msi, nil
                        );
                    }
                }
            } else {
                // Freeform sticker overlay (associatedMessageType = 1000)
                NSAttributedString *attrText = [[NSAttributedString alloc]
                    initWithString:@"\ufffc"
                        attributes:@{
                            @"__kIMMessagePartAttributeName": @0,
                            @"__kIMFileTransferGUIDAttributeName": transferGuid
                        }];
                NSString *assocGuid = [NSString stringWithFormat:@"p:%ld/%@", (long)partIndex, selectedGuid];
                Class imMessageCls = objc_getClass("IMMessage");
                SEL initAssocSel = sel_registerName(
                    "initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:"
                    "associatedMessageGUID:associatedMessageType:associatedMessageRange:messageSummaryInfo:"
                );
                if (imMessageCls && [imMessageCls instancesRespondToSelector:initAssocSel]) {
                    msg = ((id(*)(
                        id, SEL, id, id, id, id, id, unsigned long long, id, id, id, id, long long, NSRange, id
                    ))objc_msgSend)(
                        [imMessageCls alloc],
                        initAssocSel,
                        nil,
                        [NSDate date],
                        attrText,
                        nil,
                        @[transferGuid],
                        0x100005ULL,
                        nil,
                        nil,
                        nil,
                        assocGuid,
                        kIMAssociatedMessageTypeSticker,
                        partRange,
                        msi
                    );
                }
            }

            if (!msg) {
                sendTransactionResponse(transactionId, nil, @"Failed to construct sticker IMMessage");
                return;
            }
            ((void(*)(id, SEL, id))objc_msgSend)(chat, sel_registerName("sendMessage:"), msg);
            NSString *outGuid = [msg respondsToSelector:sel_registerName("guid")]
                ? ((id(*)(id, SEL))objc_msgSend)(msg, sel_registerName("guid"))
                : @"";
            os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
            os_log(log, "BLUEBUBBLESHELPER: Sent attached sticker (tapback=%d) guid=%{public}@", isTapback, outGuid);
            sendTransactionResponse(transactionId, outGuid, nil);
        });
        return;
    }

    // Standalone sticker message
    NSAttributedString *attrText = [[NSAttributedString alloc]
        initWithString:@"\ufffc"
            attributes:@{
                @"__kIMMessagePartAttributeName": @0,
                @"__kIMFileTransferGUIDAttributeName": transferGuid
            }];
    Class imMessageCls = objc_getClass("IMMessage");
    SEL initPlainSel = sel_registerName(
        "initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:"
    );
    id msg = nil;
    if (imMessageCls && [imMessageCls instancesRespondToSelector:initPlainSel]) {
        msg = ((id(*)(id, SEL, id, id, id, id, id, unsigned long long, id, id, id))objc_msgSend)(
            [imMessageCls alloc],
            initPlainSel,
            nil,
            [NSDate date],
            attrText,
            nil,
            @[transferGuid],
            5ULL,
            nil,
            nil,
            nil
        );
    }
    if (!msg) {
        sendTransactionResponse(transactionId, nil, @"Failed to construct standalone sticker IMMessage");
        return;
    }
    ((void(*)(id, SEL, id))objc_msgSend)(chat, sel_registerName("sendMessage:"), msg);
    NSString *outGuid = [msg respondsToSelector:sel_registerName("guid")]
        ? ((id(*)(id, SEL))objc_msgSend)(msg, sel_registerName("guid"))
        : @"";
    os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");
    os_log(log, "BLUEBUBBLESHELPER: Sent standalone sticker guid=%{public}@", outGuid);
    sendTransactionResponse(transactionId, outGuid, nil);
}

// ---------------------------------------------------------------------------
// Swizzled -[BlueBubblesHelper handleMessage:message:] Dispatcher
// ---------------------------------------------------------------------------

static BOOL tryHandleCustomAction(NSString *rawMessage) {
    if (!rawMessage || ![rawMessage isKindOfClass:[NSString class]]) {
        return NO;
    }
    if ([rawMessage rangeOfString:kCustomEffectPrefix].location == NSNotFound) {
        return NO;
    }
    NSString *trimmed = rawMessage;
    NSRange nlRange = [trimmed rangeOfString:@"\n"];
    if (nlRange.location != NSNotFound) {
        trimmed = [trimmed substringToIndex:nlRange.location + 1];
    }
    NSData *jsonData = [trimmed dataUsingEncoding:NSUTF8StringEncoding];
    if (!jsonData) {
        return NO;
    }
    NSDictionary *packet = [NSJSONSerialization JSONObjectWithData:jsonData options:0 error:nil];
    if (![packet isKindOfClass:[NSDictionary class]]) {
        return NO;
    }
    NSString *action = [packet[@"action"] isKindOfClass:[NSString class]] ? packet[@"action"] : nil;
    NSString *transactionId = [packet[@"transactionId"] isKindOfClass:[NSString class]] ? packet[@"transactionId"] : nil;
    NSDictionary *data = [packet[@"data"] isKindOfClass:[NSDictionary class]] ? packet[@"data"] : nil;
    if (!action || !data) {
        return NO;
    }

    NSString *effectId = [data[@"effectId"] isKindOfClass:[NSString class]] ? data[@"effectId"] : nil;
    if (!effectId || ![effectId hasPrefix:kCustomEffectPrefix]) {
        return NO;
    }

    NSString *specJsonStr = [effectId substringFromIndex:kCustomEffectPrefix.length];
    NSData *specData = [specJsonStr dataUsingEncoding:NSUTF8StringEncoding];
    NSDictionary *spec = specData ? [NSJSONSerialization JSONObjectWithData:specData options:0 error:nil] : nil;
    if (![spec isKindOfClass:[NSDictionary class]]) {
        return NO;
    }

    NSString *chatGuid = [data[@"chatGuid"] isKindOfClass:[NSString class]] ? data[@"chatGuid"] : nil;
    if (!chatGuid || chatGuid.length == 0) {
        return NO;
    }

    void (^work)(void) = ^{
        id chat = getIMChat(chatGuid, transactionId);
        if (!chat) {
            return;
        }
        NSString *mode = [spec[@"mode"] isKindOfClass:[NSString class]] ? spec[@"mode"] : @"";
        if ([action isEqualToString:@"send-attachment"]) {
            handleStickerAttachment(chat, transactionId, data, spec);
        } else if ([action isEqualToString:@"send-message"]) {
            if ([mode hasPrefix:@"poll_"]) {
                handlePollAction(chat, transactionId, data, spec);
            } else {
                handleFormattedOrScheduledMessage(chat, transactionId, data, spec);
            }
        }
    };

    if ([NSThread isMainThread]) {
        work();
    } else {
        dispatch_async(dispatch_get_main_queue(), work);
    }
    return YES;
}

static void swizzled_handleMessage(id self, SEL _cmd, id netController, NSString *message) {
    if (tryHandleCustomAction(message)) {
        return;
    }
    ((void(*)(id, SEL, id, NSString *))g_origHandleMessage)(self, _cmd, netController, message);
}

// ---------------------------------------------------------------------------
// macOS 26/27 (Tahoe) IMChat Edit Compatibility Shim
// ---------------------------------------------------------------------------
// BlueBubblesHelperOrig.dylib calls:
//   -[IMChat editMessageItem:atPartIndex:withNewPartText:backwardCompatabilityText:]
// On macOS 26/27+, IMChat replaced that selector with:
//   -[IMChat editMessageItem:atPartIndex:withNewPartText:newPartTranslation:backwardCompatabilityText:]
// and requires __kIMMessagePartAttributeName on the NSAttributedString.

static NSAttributedString *ensureEditedPartAttributes(id attrOrStr, NSInteger partIndex) {
    NSString *plain = nil;
    NSDictionary *existingAttrs = nil;
    if ([attrOrStr isKindOfClass:[NSAttributedString class]]) {
        NSAttributedString *as = (NSAttributedString *)attrOrStr;
        plain = as.string ?: @"";
        if (as.length > 0) {
            existingAttrs = [as attributesAtIndex:0 effectiveRange:NULL];
        }
    } else if ([attrOrStr isKindOfClass:[NSString class]]) {
        plain = (NSString *)attrOrStr;
    } else {
        plain = @"";
    }
    NSMutableDictionary *attrs = existingAttrs
        ? [existingAttrs mutableCopy]
        : [NSMutableDictionary dictionary];
    if (!attrs[@"__kIMMessagePartAttributeName"]) {
        attrs[@"__kIMMessagePartAttributeName"] = @(partIndex >= 0 ? partIndex : 0);
    }
    return [[NSAttributedString alloc] initWithString:plain attributes:attrs];
}

static void compat_editMessageItem(
    id self,
    SEL _cmd,
    id messageItem,
    NSInteger partIndex,
    id newPartText,
    id backwardCompatText
) {
    SEL newSel = sel_registerName(
        "editMessageItem:atPartIndex:withNewPartText:newPartTranslation:backwardCompatabilityText:"
    );
    if ([self respondsToSelector:newSel]) {
        NSAttributedString *fixedNewText = ensureEditedPartAttributes(newPartText, partIndex);
        NSAttributedString *fixedBcText = ensureEditedPartAttributes(backwardCompatText, partIndex);
        ((void(*)(id, SEL, id, NSInteger, id, id, id))objc_msgSend)(
            self, newSel, messageItem, partIndex, fixedNewText, nil, fixedBcText
        );
    }
}

// ---------------------------------------------------------------------------
// Dylib Constructor
// ---------------------------------------------------------------------------

__attribute__((constructor))
static void initBlueBubblesEmojiTapbackSwizzle(void) {
    @autoreleasepool {
        os_log_t log = os_log_create("com.bluebubbles.messaging", "DEBUG");

        Dl_info info;
        NSString *origPath = @"/Applications/BlueBubbles.app/Contents/Resources/appResources/private-api/macos11/BlueBubblesHelperOrig.dylib";
        if (dladdr((const void *)&initBlueBubblesEmojiTapbackSwizzle, &info) && info.dli_fname) {
            NSString *selfPath = [NSString stringWithUTF8String:info.dli_fname];
            NSString *dir = [selfPath stringByDeletingLastPathComponent];
            if (dir.length > 0) {
                origPath = [dir stringByAppendingPathComponent:@"BlueBubblesHelperOrig.dylib"];
            }
        }

        void *handle = dlopen([origPath fileSystemRepresentation], RTLD_NOW | RTLD_GLOBAL);
        if (!handle) {
            os_log_error(log, "BLUEBUBBLESHELPER: Failed to dlopen %{public}@: %{public}s", origPath, dlerror());
            return;
        }

        Class helperCls = objc_getClass("BlueBubblesHelper");
        if (helperCls) {
            Method parseMethod = class_getClassMethod(helperCls, sel_registerName("parseReactionType:"));
            if (parseMethod) {
                g_origParseReactionType = method_setImplementation(parseMethod, (IMP)swizzled_parseReactionType);
            }
            Method verbMethod = class_getClassMethod(helperCls, sel_registerName("reactionToVerb:"));
            if (verbMethod) {
                g_origReactionToVerb = method_setImplementation(verbMethod, (IMP)swizzled_reactionToVerb);
            }
            Method handleMsgMethod = class_getInstanceMethod(helperCls, sel_registerName("handleMessage:message:"));
            if (handleMsgMethod) {
                g_origHandleMessage = method_setImplementation(handleMsgMethod, (IMP)swizzled_handleMessage);
            }
        }

        Class imMessageCls = objc_getClass("IMMessage");
        SEL initSel = sel_registerName(
            "initWithSender:time:text:messageSubject:fileTransferGUIDs:flags:error:guid:subject:"
            "associatedMessageGUID:associatedMessageType:associatedMessageRange:messageSummaryInfo:"
        );
        if (imMessageCls) {
            Method initMethod = class_getInstanceMethod(imMessageCls, initSel);
            if (initMethod) {
                g_origInitWithSender = method_setImplementation(initMethod, (IMP)swizzled_initWithSender);
            }
        }

        Class imChatCls = objc_getClass("IMChat");
        BOOL addedEditCompat = NO;
        if (imChatCls) {
            SEL oldEditSel = sel_registerName(
                "editMessageItem:atPartIndex:withNewPartText:backwardCompatabilityText:"
            );
            SEL newEditSel = sel_registerName(
                "editMessageItem:atPartIndex:withNewPartText:newPartTranslation:backwardCompatabilityText:"
            );
            if (![imChatCls instancesRespondToSelector:oldEditSel] &&
                [imChatCls instancesRespondToSelector:newEditSel]) {
                addedEditCompat = class_addMethod(
                    imChatCls, oldEditSel, (IMP)compat_editMessageItem, "v48@0:8@16q24@32@40"
                );
            }
        }

        os_log(log, "BLUEBUBBLESHELPER: Emoji tapback swizzle initialized (parse=%d, verb=%d, immsg=%d, handle=%d, editCompat=%d)",
               g_origParseReactionType != NULL,
               g_origReactionToVerb != NULL,
               g_origInitWithSender != NULL,
               g_origHandleMessage != NULL,
               addedEditCompat);
    }
}
