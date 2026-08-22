//======================================================================
// DeckReader.cpp
//
// Created  30-Nov-1992  R. Salcedo
// Revised  05-Aug-1994  SWOF forwarded to KRLOAD
// Revised  14-Jul-1998  INCLUDE nesting
//======================================================================
#include "DeckReader.hpp"
#include "FortranBridge.hpp"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>

static const int MAX_INCLUDE_DEPTH = 4;
static const int MAX_TABLE_ROWS    = 50;

// Nested INCLUDE support. Pre-STL, so a fixed depth array of FILE*.
struct DeckReader::FileStack {
    FILE* files[MAX_INCLUDE_DEPTH];
    int   lines[MAX_INCLUDE_DEPTH];
    int   depth;
};

//----------------------------------------------------------------------
DeckReader::DeckReader()
{
    m_stack = new FileStack;
    m_stack->depth = 0;
    for (int i = 0; i < MAX_INCLUDE_DEPTH; i++) {
        m_stack->files[i] = 0;
        m_stack->lines[i] = 0;
    }

    m_nx = 0;
    m_ny = 0;
    m_nz = 0;
    m_section = SECTION_NONE;
    m_wells = 0;
    m_error_line = 0;
    m_api = 35.0;
    m_gas_sg = 0.65;
    m_temp_f = 180.0;
    m_nfields = 0;
    m_card[0] = '\0';
    m_error[0] = '\0';
}

//----------------------------------------------------------------------
DeckReader::~DeckReader()
{
    while (m_stack->depth > 0) {
        pop_include();
    }
    delete m_stack;
    m_stack = 0;
}

//----------------------------------------------------------------------
int DeckReader::push_include(const char* filename)
{
    if (m_stack->depth >= MAX_INCLUDE_DEPTH) {
        strcpy(m_error, "INCLUDE nesting deeper than 4 levels");
        return -1;
    }
    FILE* f = fopen(filename, "r");
    if (f == 0) {
        sprintf(m_error, "cannot open '%.80s'", filename);
        return -1;
    }
    m_stack->files[m_stack->depth] = f;
    m_stack->lines[m_stack->depth] = 0;
    m_stack->depth++;
    return 0;
}

int DeckReader::pop_include()
{
    if (m_stack->depth <= 0) {
        return -1;
    }
    m_stack->depth--;
    fclose(m_stack->files[m_stack->depth]);
    m_stack->files[m_stack->depth] = 0;
    return 0;
}

//----------------------------------------------------------------------
// Strip the "--" comment tail and trailing whitespace, in place.
//----------------------------------------------------------------------
void DeckReader::strip_comment(char* card) const
{
    char* dash = strstr(card, "--");
    if (dash != 0) {
        *dash = '\0';
    }
    int n = (int)strlen(card);
    while (n > 0 && (card[n - 1] == '\n' || card[n - 1] == '\r'
                     || card[n - 1] == ' '  || card[n - 1] == '\t')) {
        card[--n] = '\0';
    }
}

//----------------------------------------------------------------------
// Split m_card into blank separated fields. A trailing '/' terminates
// the record and is dropped. Returns the field count.
//----------------------------------------------------------------------
int DeckReader::split_fields(char* card)
{
    m_nfields = 0;
    char* p = card;

    while (*p != '\0' && m_nfields < 16) {
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        if (*p == '/') {
            break;
        }
        char* start = p;
        while (*p != '\0' && *p != ' ' && *p != '\t' && *p != '/') {
            p++;
        }
        int len = (int)(p - start);
        if (len > 31) {
            len = 31;
        }
        memcpy(m_fields[m_nfields], start, len);
        m_fields[m_nfields][len] = '\0';
        m_nfields++;
    }
    return m_nfields;
}

//----------------------------------------------------------------------
// Read the next non blank, non comment card, following INCLUDEs.
// Returns 1 on success, 0 at end of the outermost file.
//----------------------------------------------------------------------
int DeckReader::next_card()
{
    while (m_stack->depth > 0) {
        FILE* f = m_stack->files[m_stack->depth - 1];
        if (fgets(m_card, (int)sizeof(m_card), f) == 0) {
            pop_include();
            continue;
        }
        m_stack->lines[m_stack->depth - 1]++;
        strip_comment(m_card);
        if (m_card[0] == '\0') {
            continue;
        }
        int blank = 1;
        for (const char* p = m_card; *p != '\0'; p++) {
            if (*p != ' ' && *p != '\t') {
                blank = 0;
                break;
            }
        }
        if (!blank) {
            return 1;
        }
    }
    return 0;
}

//----------------------------------------------------------------------
int DeckReader::read(const char* filename)
{
    if (push_include(filename) != 0) {
        m_error_line = 1;
        return 1;
    }

    while (next_card()) {
        char keyword[32];
        char scratch[132];
        strcpy(scratch, m_card);
        split_fields(scratch);
        if (m_nfields == 0) {
            continue;
        }
        strcpy(keyword, m_fields[0]);
        for (char* p = keyword; *p != '\0'; p++) {
            *p = (char)toupper((unsigned char)*p);
        }

        if (handle_keyword(keyword) != 0) {
            m_error_line = m_stack->depth > 0
                         ? m_stack->lines[m_stack->depth - 1]
                         : 0;
            return m_error_line > 0 ? m_error_line : 1;
        }
    }
    return 0;
}

//----------------------------------------------------------------------
int DeckReader::handle_keyword(const char* keyword)
{
    // Section markers.
    if (strcmp(keyword, "RUNSPEC") == 0)  { m_section = SECTION_RUNSPEC;  return 0; }
    if (strcmp(keyword, "GRID") == 0)     { m_section = SECTION_GRID;     return 0; }
    if (strcmp(keyword, "PROPS") == 0)    { m_section = SECTION_PROPS;    return 0; }
    if (strcmp(keyword, "SOLUTION") == 0) { m_section = SECTION_SOLUTION; return 0; }
    if (strcmp(keyword, "SCHEDULE") == 0) { m_section = SECTION_SCHEDULE; return 0; }
    if (strcmp(keyword, "END") == 0)      { while (m_stack->depth > 0) pop_include(); return 0; }

    if (strcmp(keyword, "FIELD") == 0)  { m_units.set_system(UNIT_FIELD);  return 0; }
    if (strcmp(keyword, "METRIC") == 0) { m_units.set_system(UNIT_METRIC); return 0; }
    if (strcmp(keyword, "LAB") == 0)    { m_units.set_system(UNIT_LAB);    return 0; }

    if (strcmp(keyword, "INCLUDE") == 0) {
        if (!next_card()) {
            strcpy(m_error, "INCLUDE with no filename");
            return -1;
        }
        char name[132];
        strcpy(name, m_card);
        split_fields(name);
        if (m_nfields < 1) {
            strcpy(m_error, "INCLUDE with no filename");
            return -1;
        }
        // Strip surrounding quotes if the deck used them.
        char* f = m_fields[0];
        if (*f == '\'' || *f == '"') {
            f++;
            int n = (int)strlen(f);
            if (n > 0 && (f[n - 1] == '\'' || f[n - 1] == '"')) {
                f[n - 1] = '\0';
            }
        }
        return push_include(f);
    }

    if (strcmp(keyword, "DIMENS") == 0) {
        if (!next_card()) {
            strcpy(m_error, "DIMENS with no data record");
            return -1;
        }
        char buf[132];
        strcpy(buf, m_card);
        split_fields(buf);
        if (m_nfields < 3) {
            strcpy(m_error, "DIMENS needs NX NY NZ");
            return -1;
        }
        m_nx = atoi(m_fields[0]);
        m_ny = atoi(m_fields[1]);
        m_nz = atoi(m_fields[2]);
        if (m_nx < 1 || m_ny < 1 || m_nz < 1) {
            strcpy(m_error, "DIMENS values must be positive");
            return -1;
        }
        int mx = m_nx, my = m_ny, mz = m_nz;
        F77_NAME(simini, SIMINI)(&mx, &my, &mz);
        if (F77_NAME(diag, DIAG).ierr != 0) {
            sprintf(m_error, "SIMINI rejected the grid (IERR = %d)",
                    F77_NAME(diag, DIAG).ierr);
            F77_NAME(diag, DIAG).ierr = 0;
            return -1;
        }
        return 0;
    }

    // GRID property arrays. Each is cell_count() reals terminated by '/'.
    struct { const char* kw; int code; } props[] = {
        { "PORO",   1 }, { "PERMX",  2 }, { "PERMY",  3 },
        { "PERMZ",  4 }, { "DZ",     5 }, { "TOPS",   6 },
        { "NTG",    7 }, { "ACTNUM", 8 }, { 0, 0 }
    };
    for (int i = 0; props[i].kw != 0; i++) {
        if (strcmp(keyword, props[i].kw) != 0) {
            continue;
        }
        int n = cell_count();
        if (n <= 0) {
            strcpy(m_error, "grid property before DIMENS");
            return -1;
        }
        double* buf = new double[n];
        if (read_real_array(buf, n) != 0) {
            delete[] buf;
            return -1;
        }
        m_units.convert_array(buf, n, props[i].code);

        int code = props[i].code;
        int count = n;
        F77_NAME(grdset, GRDSET)(&code, buf, &count);
        delete[] buf;

        if (F77_NAME(diag, DIAG).ierr != 0) {
            sprintf(m_error, "GRDSET rejected %s (IERR = %d)",
                    props[i].kw, F77_NAME(diag, DIAG).ierr);
            F77_NAME(diag, DIAG).ierr = 0;
            return -1;
        }
        return 0;
    }

    if (strcmp(keyword, "SWOF") == 0) {
        return read_swof_table();
    }
    if (strcmp(keyword, "WELSPECS") == 0) {
        return read_welspecs();
    }
    if (strcmp(keyword, "TSTEP") == 0) {
        return read_tstep();
    }

    if (strcmp(keyword, "DENSITY") == 0 || strcmp(keyword, "PVTO") == 0) {
        // Consume the record; the correlations are used instead of the
        // tabulated PVT. A deck carrying both has always silently
        // preferred the correlations - documented but surprising.
        while (next_card()) {
            if (strchr(m_card, '/') != 0) {
                break;
            }
        }
        return 0;
    }

    // Unknown keywords are skipped, matching the 1992 reader. Decks in
    // the field carry vendor keywords we never implemented.
    return 0;
}

//----------------------------------------------------------------------
// Read count reals, honouring the n*value repeat notation.
//----------------------------------------------------------------------
int DeckReader::read_real_array(double* target, int count)
{
    int filled = 0;

    while (filled < count) {
        if (!next_card()) {
            sprintf(m_error, "array ended after %d of %d values",
                    filled, count);
            return -1;
        }
        char buf[132];
        strcpy(buf, m_card);
        int nf = split_fields(buf);

        for (int i = 0; i < nf; i++) {
            const char* tok = m_fields[i];
            const char* star = strchr(tok, '*');
            int repeat = 1;
            double value = 0.0;

            if (star != 0) {
                char num[32];
                int len = (int)(star - tok);
                if (len > 31) len = 31;
                memcpy(num, tok, len);
                num[len] = '\0';
                repeat = atoi(num);
                value  = atof(star + 1);
                if (repeat < 1) {
                    sprintf(m_error, "bad repeat count in '%.20s'", tok);
                    return -1;
                }
            } else {
                value = atof(tok);
            }

            for (int r = 0; r < repeat; r++) {
                if (filled >= count) {
                    sprintf(m_error, "array has more than %d values", count);
                    return -1;
                }
                target[filled++] = value;
            }
        }
        if (strchr(m_card, '/') != 0) {
            break;
        }
    }

    if (filled != count) {
        sprintf(m_error, "array has %d values, expected %d", filled, count);
        return -1;
    }
    return 0;
}

//----------------------------------------------------------------------
// SWOF: four columns, SW KRW KROW PCOW, terminated by '/'.
//----------------------------------------------------------------------
int DeckReader::read_swof_table()
{
    double sw[MAX_TABLE_ROWS];
    double krw[MAX_TABLE_ROWS];
    double kro[MAX_TABLE_ROWS];
    double pcw[MAX_TABLE_ROWS];
    int rows = 0;

    while (next_card()) {
        if (m_card[0] == '/') {
            break;
        }
        char buf[132];
        strcpy(buf, m_card);
        int nf = split_fields(buf);
        if (nf == 0) {
            break;
        }
        if (nf < 4) {
            sprintf(m_error, "SWOF row %d has %d columns, expected 4",
                    rows + 1, nf);
            return -1;
        }
        if (rows >= MAX_TABLE_ROWS) {
            sprintf(m_error, "SWOF has more than %d rows", MAX_TABLE_ROWS);
            return -1;
        }
        sw [rows] = atof(m_fields[0]);
        krw[rows] = atof(m_fields[1]);
        kro[rows] = atof(m_fields[2]);
        pcw[rows] = m_units.pressure(atof(m_fields[3]));
        rows++;

        if (strchr(m_card, '/') != 0) {
            break;
        }
    }

    if (rows < 2) {
        strcpy(m_error, "SWOF needs at least two rows");
        return -1;
    }

    int n = rows;
    F77_NAME(krload, KRLOAD)(sw, krw, kro, pcw, &n);
    if (F77_NAME(diag, DIAG).ierr != 0) {
        sprintf(m_error, "KRLOAD rejected SWOF (IERR = %d)",
                F77_NAME(diag, DIAG).ierr);
        F77_NAME(diag, DIAG).ierr = 0;
        return -1;
    }
    return 0;
}

//----------------------------------------------------------------------
// WELSPECS: one record per well, terminated by a bare '/'.
//----------------------------------------------------------------------
int DeckReader::read_welspecs()
{
    while (next_card()) {
        if (m_card[0] == '/') {
            break;
        }
        char buf[132];
        strcpy(buf, m_card);
        int nf = split_fields(buf);
        if (nf < 4) {
            sprintf(m_error, "WELSPECS record has %d fields, expected 4", nf);
            return -1;
        }

        char name[9];
        memset(name, ' ', 8);
        name[8] = '\0';
        const char* src = m_fields[0];
        if (*src == '\'') {
            src++;
        }
        int len = (int)strlen(src);
        if (len > 0 && src[len - 1] == '\'') {
            len--;
        }
        if (len > 8) {
            len = 8;
        }
        memcpy(name, src, len);

        int ityp = 1;
        int iw   = atoi(m_fields[2]);
        int jw   = atoi(m_fields[3]);
        int k1   = 1;
        int k2   = m_nz;
        double rw   = 0.354;
        double skin = 0.0;
        int index = 0;

        F77_NAME(weladd, WELADD)(name, &ityp, &rw, &skin, &iw, &jw,
                                 &k1, &k2, &index, 8);
        if (F77_NAME(diag, DIAG).ierr != 0) {
            sprintf(m_error, "WELADD rejected '%.8s' (IERR = %d)",
                    name, F77_NAME(diag, DIAG).ierr);
            F77_NAME(diag, DIAG).ierr = 0;
            return -1;
        }
        m_wells++;
    }
    return 0;
}

//----------------------------------------------------------------------
// TSTEP: a list of step lengths in days. Recorded, not executed - the
// driver decides when to run them.
//----------------------------------------------------------------------
int DeckReader::read_tstep()
{
    while (next_card()) {
        if (m_card[0] == '/') {
            break;
        }
        if (strchr(m_card, '/') != 0) {
            break;
        }
    }
    return 0;
}

//----------------------------------------------------------------------
int DeckReader::nx() const { return m_nx; }
int DeckReader::ny() const { return m_ny; }
int DeckReader::nz() const { return m_nz; }
int DeckReader::cell_count() const { return m_nx * m_ny * m_nz; }
int DeckReader::unit_system() const { return m_units.system(); }
int DeckReader::well_count() const { return m_wells; }

double DeckReader::api_gravity() const { return m_api; }
double DeckReader::gas_gravity() const { return m_gas_sg; }
double DeckReader::reservoir_temperature() const { return m_temp_f; }

int DeckReader::has_dimensions() const
{
    return (m_nx > 0 && m_ny > 0 && m_nz > 0) ? 1 : 0;
}

const char* DeckReader::error_text() const { return m_error; }
int DeckReader::error_line() const { return m_error_line; }
