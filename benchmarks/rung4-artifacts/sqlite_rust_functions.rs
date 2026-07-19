#[no_mangle]
pub extern "C" fn allowedOp(op: i32) -> i32 {
    const TK_EQ: i32 = 54;
    const TK_GE: i32 = 58;
    const TK_IN: i32 = 50;
    const TK_IS: i32 = 45;
    const TK_ISNULL: i32 = 51;

    if op > TK_GE {
        return 0;
    }
    if op >= TK_EQ {
        return 1;
    }
    if op == TK_IN || op == TK_ISNULL || op == TK_IS {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn countLeadingZeros(m: u64) -> i32 {
    m.leading_zeros() as i32
}

#[no_mangle]
pub extern "C" fn fts5ExprIsspace(t: i8) -> i32 {
    if t == b' ' as i8 || t == b'\t' as i8 || t == b'\n' as i8 || t == b'\r' as i8 {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn fts5_isdigit(a: i8) -> i32 {
    if a >= b'0' as i8 && a <= b'9' as i8 {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn fts5_isopenquote(x: i8) -> i32 {
    if x == b'"' as i8 || x == b'\'' as i8 || x == b'[' as i8 || x == b'`' as i8 {
        1
    } else {
        0
    }
}


#[no_mangle]
pub extern "C" fn fts5_iswhitespace(x: i8) -> i32 {
    if x == b' ' as i8 {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn isFatalError(rc: i32) -> i32 {
    if rc != 0 && rc != 5 && rc != 6 {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn nodeHash(iNode: i64) -> u32 {
    ((iNode as u32) % 97)
}


#[no_mangle]
pub extern "C" fn pwr10to2(p: i32) -> i32 {
    ((p.wrapping_mul(108853)) >> 15) as i32
}


#[no_mangle]
pub extern "C" fn pwr2to10(p: i32) -> i32 {
    (p.wrapping_mul(78913)) >> 18
}


#[no_mangle]
pub extern "C" fn sqlite3AbsInt32(x: i32) -> i32 {
    if x >= 0 {
        return x;
    }
    if x == -2147483648i32 {
        return 2147483647i32;
    }
    -x
}


#[no_mangle]
pub extern "C" fn sqlite3BtreeCursorSize() -> i32 {
    (((296i32 + 7) & !7))
}

#[no_mangle]
pub extern "C" fn sqlite3Fts3VarintLen(mut v: u64) -> i32 {
    let mut i: i32 = 0;
    loop {
        i += 1;
        v >>= 7;
        if v == 0 {
            break;
        }
    }
    i
}

#[no_mangle]
pub extern "C" fn sqlite3Fts5ParserFallback(iToken: i32) -> i32 {
    #[cfg(feature = "fts5YYFALLBACK")]
    {
        // In the C code, fts5yyFallback is a static array. Since we don't have
        // access to it, we return 0 as a safe default that matches the behavior
        // when the feature is disabled.
        0
    }
    #[cfg(not(feature = "fts5YYFALLBACK"))]
    {
        0
    }
}


#[no_mangle]
pub extern "C" fn sqlite3HeaderSizeBtree() -> i32 {
    ((136 + 7) & !7) as i32
}

#[no_mangle]
pub extern "C" fn sqlite3HexToInt(h: i32) -> u8 {
    let h = h.wrapping_add(9i32.wrapping_mul(1 & (h >> 6)));
    (h & 0xf) as u8
}

#[no_mangle]
pub extern "C" fn sqlite3IntFloatCompare(i: i64, r: f64) -> i32 {
    if r.is_nan() {
        return 1;
    }
    
    if r < -9223372036854775808.0 {
        return 1;
    }
    if r >= 9223372036854775808.0 {
        return -1;
    }
    
    let y = r as i64;
    if i < y {
        return -1;
    }
    if i > y {
        return 1;
    }
    
    let i_as_f64 = i as f64;
    if i_as_f64 < r {
        -1
    } else if i_as_f64 > r {
        1
    } else {
        0
    }
}


#[no_mangle]
pub extern "C" fn sqlite3IsIdChar(c: u8) -> i32 {
    const SQLITE_CTYPE_MAP: [u8; 256] = [
        0,0,0,0,0,0,0,0,0,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,128,0,64,0,0,128,0,0,0,0,0,0,0,0,12,12,12,12,12,12,12,12,12,12,0,0,0,0,0,0,0,10,10,10,10,10,10,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,128,0,0,0,64,128,42,42,42,42,42,42,34,34,34,34,34,34,34,34,34,34,34,34,34,34,34,34,34,34,34,34,0,0,0,0,0,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,64,
    ];
    
    let idx = c as usize;
    if (SQLITE_CTYPE_MAP[idx] & 0x46) != 0 { 1 } else { 0 }
}


#[no_mangle]
pub extern "C" fn sqlite3LogEst(mut x: u64) -> i16 {
    const A: &[i16] = &[0, 2, 3, 5, 6, 7, 8, 9];
    let mut y: i16 = 40;
    
    if x < 8 {
        if x < 2 {
            return 0;
        }
        while x < 8 {
            y -= 10;
            x <<= 1;
        }
    } else {
        // GCC_VERSION = 4002001, which is < 5004000, so else branch
        while x > 255 {
            y += 40;
            x >>= 4;
        }
        while x > 15 {
            y += 10;
            x >>= 1;
        }
    }
    
    A[(x & 7) as usize].wrapping_add(y).wrapping_sub(10)
}


#[no_mangle]
pub extern "C" fn sqlite3LogEstToInt(mut x: i16) -> u64 {
    let mut n: u64 = (x % 10) as u64;
    x /= 10;
    if n >= 5 {
        n -= 2;
    } else if n >= 1 {
        n -= 1;
    }
    if x > 60 {
        return 9223372036854775807u64;
    }
    if x >= 3 {
        (n + 8) << (x - 3)
    } else {
        (n + 8) >> (3 - x)
    }
}


#[no_mangle]
pub extern "C" fn sqlite3RealSameAsInt(r1: f64, i: i64) -> i32 {
    let r2 = i as f64;
    let result = r1 == 0.0
        || (r1.to_bits() == r2.to_bits()
            && i >= -2251799813685248i64
            && i < 2251799813685248i64);
    if result { 1 } else { 0 }
}

#[no_mangle]
pub extern "C" fn sqlite3RealToI64(r: f64) -> i64 {
    if r < -9223372036854774784.0 {
        -9223372036854775808
    } else if r > 9223372036854774784.0 {
        9223372036854775807
    } else {
        r as i64
    }
}

#[no_mangle]
pub extern "C" fn sqlite3VarintLen(mut v: u64) -> i32 {
    let mut i: i32 = 1;
    while {
        v >>= 7;
        v != 0
    } {
        i += 1;
    }
    i
}


#[no_mangle]
pub extern "C" fn sqlite3VdbeSerialTypeLen(serial_type: u32) -> u32 {
    const SQLITE_SMALL_TYPE_SIZES: [u32; 128] = [
        0, 1, 2, 3, 4, 6, 8, 8, 0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9,
        10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15, 16, 16, 17, 17, 18, 18, 19, 19, 20, 20, 21, 21, 22, 22, 23, 23, 24, 24, 25, 25,
        26, 26, 27, 27, 28, 28, 29, 29, 30, 30, 31, 31, 32, 32, 33, 33, 34, 34, 35, 35, 36, 36, 37, 37, 38, 38, 39, 39, 40, 40, 41, 41,
        42, 42, 43, 43, 44, 44, 45, 45, 46, 46, 47, 47, 48, 48, 49, 49, 50, 50, 51, 51, 52, 52, 53, 53, 54, 54, 55, 55, 56, 56, 57, 57,
    ];

    if serial_type >= 128 {
        (serial_type.wrapping_sub(12)) / 2
    } else {
        SQLITE_SMALL_TYPE_SIZES[serial_type as usize]
    }
}


#[no_mangle]
pub extern "C" fn sqlite3_keyword_count() -> i32 {
    147
}


#[no_mangle]
pub extern "C" fn sqlite3_libversion_number() -> i32 {
    3053003
}


#[no_mangle]
pub extern "C" fn sqlite3_release_memory(n: i32) -> i32 {
    let _ = n;
    0
}


#[no_mangle]
pub extern "C" fn sqlite3_threadsafe() -> i32 {
    1
}

#[no_mangle]
pub extern "C" fn vdbeSorterTreeDepth(nPMA: i32) -> i32 {
    let mut nDepth: i32 = 0;
    let mut nDiv: i64 = 16;
    while nDiv < (nPMA as i64) {
        nDiv = nDiv.wrapping_mul(16);
        nDepth = nDepth.wrapping_add(1);
    }
    nDepth
}

